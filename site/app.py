import os
from flask import Flask, redirect, render_template, render_template_string, request, session, url_for
from db import (
    ensure_schema_updates,
    filter_movies_in_db,
    get_connection,
    get_dynamic_filter_options,
)

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "chave_secreta_imdb_game_2026")

ensure_schema_updates()


@app.route("/")
def index():
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT original_title, poster_url
            FROM public.movie
            WHERE is_in_top250 = TRUE
            ORDER BY rating DESC, vote_count DESC
            LIMIT 3;
        """)
        top_movies = cur.fetchall()
        cur.execute("SELECT COUNT(*) FROM public.movie WHERE is_in_top250 = TRUE;")
        total_movies = cur.fetchone()[0]
    conn.close()
    return render_template("index.html", top_movies=top_movies, total_movies=total_movies)


@app.route("/start_game", methods=["POST"])
def start_game():
    num_players = int(request.form.get("num_players", 2))
    player_names = [
        request.form.get(f"player_{i}", "").strip()
        for i in range(1, num_players + 1)
        if request.form.get(f"player_{i}", "").strip()
    ]

    if len(player_names) < 2:
        return redirect(url_for("index"))

    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO public.game_session (num_players, status)
            VALUES (%s, 'FILTERING')
            RETURNING id_session;
        """, (len(player_names),))
        session_id = cur.fetchone()[0]

        for idx, name in enumerate(player_names, start=1):
            cur.execute("""
                INSERT INTO public.session_player (id_session, player_order, player_name)
                VALUES (%s, %s, %s);
            """, (session_id, idx, name))

        cur.execute("""
            INSERT INTO public.filter_round (id_session, round_number)
            VALUES (%s, 1);
        """, (session_id,))

    conn.commit()
    conn.close()
    return redirect(url_for("session_filters", session_id=session_id))


@app.route("/session/<int:session_id>/filters", methods=["GET", "POST"])
def session_filters(session_id):
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id_round FROM public.filter_round
            WHERE id_session = %s ORDER BY round_number DESC LIMIT 1;
        """, (session_id,))
        round_row = cur.fetchone()
        if not round_row:
            conn.close()
            return redirect(url_for("index"))
        round_id = round_row[0]

        if request.method == "POST":
            id_player = int(request.form.get("id_player"))
            decades_list = request.form.getlist("decades")[:3]
            duration_list = request.form.getlist("duration")[:2]
            genres_list = request.form.getlist("genres")[:3]

            decades_str = ",".join(decades_list) if decades_list else "ANY"
            duration_str = ",".join(duration_list) if duration_list else "ANY"
            genres_str = ",".join(genres_list) if genres_list else "ANY"

            certificate = request.form.get("certificate", "ANY")
            rating = float(request.form.get("rating", 8.0))

            cur.execute("""
                INSERT INTO public.filter_choice (
                    id_round, id_player, year_choice, certificate_choice,
                    duration_choice, rating_choice, genres_choice
                ) VALUES (%s, %s, %s, %s, %s, %s, %s);
            """, (round_id, id_player, decades_str, certificate, duration_str, rating, genres_str))
            conn.commit()

        cur.execute("""
            SELECT sp.id_player, sp.player_order, sp.player_name
            FROM public.session_player sp
            WHERE sp.id_session = %s
              AND sp.id_player NOT IN (
                  SELECT id_player FROM public.filter_choice WHERE id_round = %s
              )
            ORDER BY sp.player_order ASC;
        """, (session_id, round_id))
        pending_players = cur.fetchall()

        cur.execute("SELECT COUNT(*) FROM public.session_player WHERE id_session = %s;", (session_id,))
        total_players = cur.fetchone()[0]

    conn.close()

    if pending_players:
        decades, certificates, genres = get_dynamic_filter_options()
        return render_template(
            "filters.html",
            session_id=session_id,
            player=pending_players[0],
            total_players=total_players,
            decades=decades,
            certificates=certificates,
            genres=genres,
        )

    return redirect(url_for("evaluate_filters", session_id=session_id))


def compute_merged_filters(choices, winning_category=None, winner_choice=None):
    """
    Apenas a 'winning_category' (o filtro que o vencedor escolheu defender) fica
    100% igual ao do vencedor. Todos os outros 4 filtros (mesmo os escolhidos pelos
    adversários derrotados) são misturados/unidos entre os jogadores!
    """
    # 1. Décadas (Ano)
    if winning_category == "decades" and winner_choice:
        final_decades = winner_choice["decades"]
    else:
        if any(c["decades"] == "ANY" for c in choices):
            final_decades = "ANY"
        else:
            s = set()
            for c in choices:
                s.update(c["decades"].split(","))
            final_decades = ",".join(sorted(s))

    # 2. Duração
    if winning_category == "duration" and winner_choice:
        final_duration = winner_choice["duration"]
    else:
        if any(c["duration"] == "ANY" for c in choices):
            final_duration = "ANY"
        else:
            s = set()
            for c in choices:
                s.update(c["duration"].split(","))
            final_duration = ",".join(sorted(s))

    # 3. Idade (Certificate)
    if winning_category == "certificate" and winner_choice:
        final_cert = winner_choice["certificate"]
    else:
        if any(c["certificate"] == "ANY" for c in choices):
            final_cert = "ANY"
        else:
            final_cert = ",".join(sorted({c["certificate"] for c in choices}))

    # 4. Rating (Quando misturado, usa o menor rating para abranger os filmes de todos)
    if winning_category == "rating" and winner_choice:
        final_rating = float(winner_choice["rating"])
    else:
        final_rating = min(float(c["rating"]) for c in choices)

    # 5. Géneros
    if winning_category == "genres" and winner_choice:
        final_genres = winner_choice["genres"]
    else:
        if any(c["genres"] == "ANY" for c in choices):
            final_genres = "ANY"
        else:
            s = set()
            for c in choices:
                s.update(c["genres"].split(","))
            final_genres = ",".join(sorted(s))

    return {
        "decades": final_decades,
        "duration": final_duration,
        "certificate": final_cert,
        "rating": final_rating,
        "genres": final_genres,
    }


@app.route("/session/<int:session_id>/filters/result")
def evaluate_filters(session_id):
    all_decades, _, all_genres = get_dynamic_filter_options()
    all_durations = ["<1h30", "=2h", "=2h30", ">2h30"]

    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id_round FROM public.filter_round
            WHERE id_session = %s ORDER BY round_number DESC LIMIT 1;
        """, (session_id,))
        round_id = cur.fetchone()[0]

        cur.execute("""
            SELECT sp.id_player, sp.player_name, fc.year_choice, fc.duration_choice,
                   fc.certificate_choice, fc.rating_choice, fc.genres_choice
            FROM public.filter_choice fc
            JOIN public.session_player sp ON sp.id_player = fc.id_player
            WHERE fc.id_round = %s
            ORDER BY sp.player_order ASC;
        """, (round_id,))
        rows = cur.fetchall()

        decade_sets = [set(all_decades) if r[2] == "ANY" else set(r[2].split(",")) for r in rows]
        dur_sets = [set(all_durations) if r[3] == "ANY" else set(r[3].split(",")) for r in rows]
        genre_sets = [set(all_genres) if r[6] == "ANY" else set(r[6].split(",")) for r in rows]

        common_decades = set.intersection(*decade_sets) if decade_sets else set()
        common_durations = set.intersection(*dur_sets) if dur_sets else set()
        common_genres = set.intersection(*genre_sets) if genre_sets else set()
        cert_set = {r[4] for r in rows if r[4] and r[4] != "ANY"}
        max_rating = max(float(r[5]) for r in rows)

        matching_strict = filter_movies_in_db(
            list(common_decades) if common_decades else ["__NONE__"],
            list(cert_set) if len(cert_set) <= 1 else ["__NONE__"],
            list(common_durations) if common_durations else ["__NONE__"],
            max_rating,
            list(common_genres) if common_genres else ["__NONE__"],
        )

        had_conflict = len(matching_strict) < 3

        if not had_conflict:
            choice_dicts = [
                {
                    "decades": r[2], "duration": r[3], "certificate": r[4],
                    "rating": r[5], "genres": r[6]
                }
                for r in rows
            ]
            merged = compute_merged_filters(choice_dicts)
            cur.execute("""
                UPDATE public.filter_round
                SET had_conflict = FALSE,
                    applied_decades = %s, applied_duration_code = %s,
                    applied_certificate = %s, applied_min_rating = %s, applied_genre = %s
                WHERE id_round = %s;
            """, (
                merged["decades"], merged["duration"], merged["certificate"],
                merged["rating"], merged["genres"], round_id,
            ))
            cur.execute("UPDATE public.game_session SET status = 'SWIPING' WHERE id_session = %s;", (session_id,))
        else:
            cur.execute("UPDATE public.filter_round SET had_conflict = TRUE WHERE id_round = %s;", (round_id,))
            cur.execute("UPDATE public.game_session SET status = 'TIE_FILTER' WHERE id_session = %s;", (session_id,))

    conn.commit()
    conn.close()

    return render_template(
        "filter_summary.html",
        session_id=session_id,
        choices=rows,
        had_conflict=had_conflict,
        matching_count=len(matching_strict),
    )


@app.route("/session/<int:session_id>/tictactoe/start", methods=["POST"])
def tictactoe_start(session_id):
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT id_round FROM public.filter_round WHERE id_session = %s ORDER BY round_number DESC LIMIT 1;", (session_id,))
        round_id = cur.fetchone()[0]

        cur.execute("SELECT id_player, player_name FROM public.session_player WHERE id_session = %s ORDER BY player_order ASC;", (session_id,))
        players = cur.fetchall()

        used_stakes = set()
        all_cats = ["genres", "decades", "duration", "rating", "certificate"]

        for p_id, _ in players:
            stake = request.form.get(f"stake_{p_id}", "genres")
            # Garante também no servidor que dois jogadores nunca ficam com o mesmo filtro
            if stake in used_stakes:
                for fallback in all_cats:
                    if fallback not in used_stakes:
                        stake = fallback
                        break
            used_stakes.add(stake)

            cur.execute("""
                UPDATE public.filter_choice
                SET combat_category = %s
                WHERE id_round = %s AND id_player = %s;
            """, (stake, round_id, p_id))

    conn.commit()
    conn.close()

    session[f"ttt_queue_{session_id}"] = [[p[0], p[1]] for p in players]
    return redirect(url_for("tictactoe_arena", session_id=session_id))


@app.route("/session/<int:session_id>/tictactoe")
def tictactoe_arena(session_id):
    queue_key = f"ttt_queue_{session_id}"
    if queue_key not in session:
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT id_player, player_name FROM public.session_player WHERE id_session = %s ORDER BY player_order ASC;", (session_id,))
            players = cur.fetchall()
        conn.close()
        session[queue_key] = [[p[0], p[1]] for p in players]

    queue = session[queue_key]
    if len(queue) == 1:
        return redirect(url_for("apply_winner_filter", session_id=session_id, winner_id=queue[0][0]))

    p1, p2 = queue[0], queue[1]
    match_title = "Grande Final" if len(queue) == 2 else f"Eliminatória ({len(queue)} Jogadores)"
    return render_template("tictactoe.html", session_id=session_id, p1=p1, p2=p2, match_title=match_title)


@app.route("/session/<int:session_id>/tictactoe/record", methods=["POST"])
def tictactoe_record(session_id):
    p1_id = int(request.form.get("p1_id"))
    p2_id = int(request.form.get("p2_id"))
    winner_id = int(request.form.get("winner_id"))

    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT id_round FROM public.filter_round WHERE id_session = %s ORDER BY round_number DESC LIMIT 1;", (session_id,))
        round_id = cur.fetchone()[0]

        cur.execute("""
            INSERT INTO public.tiebreaker_match (
                id_session, id_round, game_type, stage,
                player1_id, player2_id, winner_player_id
            ) VALUES (%s, %s, 'TIC_TAC_TOE', 'FILTER_CONFLICT', %s, %s, %s);
        """, (session_id, round_id, p1_id, p2_id, winner_id))
    conn.commit()
    conn.close()

    queue_key = f"ttt_queue_{session_id}"
    queue = session.get(queue_key, [])
    if len(queue) >= 2:
        p1, p2 = queue[0], queue[1]
        winner_obj = p1 if p1[0] == winner_id else p2
        session[queue_key] = queue[2:] + [winner_obj]
        session.modified = True

    return redirect(url_for("tictactoe_arena", session_id=session_id))


@app.route("/session/<int:session_id>/tictactoe/winner/<int:winner_id>")
def apply_winner_filter(session_id, winner_id):
    """
    Aplica a 100% APENAS a categoria escolhida pelo vencedor (winning_category).
    Todas as restantes categorias (incluindo as dos derrotados) ficam misturadas!
    """
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT id_round FROM public.filter_round WHERE id_session = %s ORDER BY round_number DESC LIMIT 1;", (session_id,))
        round_id = cur.fetchone()[0]

        cur.execute("""
            SELECT sp.id_player, sp.player_name, fc.year_choice, fc.duration_choice,
                   fc.certificate_choice, fc.rating_choice, fc.genres_choice, fc.combat_category
            FROM public.filter_choice fc
            JOIN public.session_player sp ON sp.id_player = fc.id_player
            WHERE fc.id_round = %s;
        """, (round_id,))
        rows = cur.fetchall()

        choice_dicts = []
        winner_choice = None
        winner_name = ""
        winning_category = None

        for r in rows:
            c_dict = {
                "id_player": r[0], "name": r[1], "decades": r[2],
                "duration": r[3], "certificate": r[4], "rating": r[5],
                "genres": r[6], "combat": r[7] or "genres",
            }
            choice_dicts.append(c_dict)
            if r[0] == winner_id:
                winner_choice = c_dict
                winner_name = r[1]
                winning_category = c_dict["combat"]

        merged = compute_merged_filters(choice_dicts, winning_category, winner_choice)

        cur.execute("""
            UPDATE public.filter_round
            SET winning_player_id = %s,
                applied_decades = %s,
                applied_duration_code = %s,
                applied_certificate = %s,
                applied_min_rating = %s,
                applied_genre = %s
            WHERE id_round = %s;
        """, (
            winner_id, merged["decades"], merged["duration"],
            merged["certificate"], merged["rating"], merged["genres"], round_id,
        ))
        cur.execute("UPDATE public.game_session SET status = 'SWIPING' WHERE id_session = %s;", (session_id,))
    conn.commit()
    conn.close()

    session.pop(f"batch_idx_{session_id}", None)

    cat_names = {
        "genres": "Género", "decades": "Ano", "duration": "Duração",
        "rating": "Pontuação", "certificate": "Restrição de Idade"
    }
    won_cat_label = cat_names.get(winning_category, winning_category)

    return render_template_string("""
    <!DOCTYPE html>
    <html lang="pt">
    <head>
        <meta charset="UTF-8">
        <title>Filtro Vencedor</title>
        <style>
            body { background:#121212; color:#fff; font-family:'Segoe UI',sans-serif; text-align:center; padding:60px 20px; }
            .card { max-width:560px; margin:0 auto; background:#1e1e1e; padding:32px; border-radius:14px; border:1px solid #f5c518; }
            h1 { color:#f5c518; margin-top:0; }
            .btn { display:inline-block; background:#f5c518; color:#000; font-weight:bold; padding:14px 28px; border-radius:8px; text-decoration:none; margin-top:20px; }
        </style>
    </head>
    <body>
        <div class="card">
            <h1>🏆 {{ winner_name }} venceu o Jogo do Galo!</h1>
            <p>Filtro exclusivo de <strong>{{ winner_name }}</strong> (100%): <strong>{{ won_cat_label }}</strong></p>
            <p style="color:#aaa; font-size:0.92rem;">Todos os outros filtros foram misturados entre o grupo:</p>
            <p style="color:#eee;">
                <strong>Ano:</strong> {{ 'Todos' if merged.decades == 'ANY' else merged.decades }} |
                <strong>Duração:</strong> {{ 'Todas' if merged.duration == 'ANY' else merged.duration }} |
                <strong>Rating:</strong> ⭐ {{ merged.rating }}+ |
                <strong>Género:</strong> {{ 'Todos' if merged.genres == 'ANY' else merged.genres }}
            </p>
            <a href="/session/{{ session_id }}/movies" class="btn">Escolher Filmes (Etapa 5) ➔</a>
        </div>
    </body>
    </html>
    """, winner_name=winner_name, won_cat_label=won_cat_label, merged=merged, session_id=session_id)


# =========================================================
# ETAPA 5: RONDAS DE 3 FILMES (GOSTO / NÃO GOSTO / JÁ VI)
# =========================================================
def get_session_movie_pool(session_id, round_id):
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT applied_decades, applied_certificate, applied_duration_code,
                   applied_min_rating, applied_genre
            FROM public.filter_round WHERE id_round = %s;
        """, (round_id,))
        row = cur.fetchone()
    conn.close()

    dec_list = row[0].split(",") if row[0] and row[0] != "ANY" else ["ANY"]
    cert_list = row[1].split(",") if row[1] and row[1] != "ANY" else ["ANY"]
    dur_list = row[2].split(",") if row[2] and row[2] != "ANY" else ["ANY"]
    min_rat = float(row[3]) if row[3] else 8.0
    gen_list = row[4].split(",") if row[4] and row[4] != "ANY" else ["ANY"]

    movies = filter_movies_in_db(dec_list, cert_list, dur_list, min_rat, gen_list)
    if len(movies) < 3:
        movies = filter_movies_in_db(["ANY"], ["ANY"], dur_list, 8.0, gen_list)
    if len(movies) < 3:
        movies = filter_movies_in_db(["ANY"], ["ANY"], ["ANY"], 8.0, ["ANY"])
    return movies


@app.route("/session/<int:session_id>/movies")
def movies_round(session_id):
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT id_round FROM public.filter_round WHERE id_session = %s ORDER BY round_number DESC LIMIT 1;", (session_id,))
        round_id = cur.fetchone()[0]

        cur.execute("SELECT id_player, player_order, player_name FROM public.session_player WHERE id_session = %s ORDER BY player_order ASC;", (session_id,))
        players = cur.fetchall()
        num_players = len(players)

        cur.execute("""
            SELECT id_movie
            FROM public.movie_choice
            WHERE id_session = %s AND id_round = %s
            GROUP BY id_movie
            HAVING COUNT(DISTINCT CASE WHEN vote_type = 1 THEN id_player END) = %s
               AND SUM(CASE WHEN vote_type IN (0, 2) THEN 1 ELSE 0 END) = 0;
        """, (session_id, round_id, num_players))
        approved_ids = [r[0] for r in cur.fetchall()]

    conn.close()

    if len(approved_ids) >= 3:
        return finalize_top3_movies(session_id, round_id, approved_ids[:3])

    all_matching = get_session_movie_pool(session_id, round_id)
    batch_key = f"batch_idx_{session_id}"
    batch_idx = session.get(batch_key, 0)

    start_pos = batch_idx * 3
    current_batch = all_matching[start_pos : start_pos + 3]

    if not current_batch:
        return finalize_top3_fallback(session_id, round_id, all_matching)

    batch_movie_ids = [m[0] for m in current_batch]

    conn = get_connection()
    with conn.cursor() as cur:
        pending_player = None
        for p in players:
            cur.execute("""
                SELECT COUNT(*) FROM public.movie_choice
                WHERE id_session = %s AND id_round = %s AND id_player = %s
                  AND id_movie = ANY(%s);
            """, (session_id, round_id, p[0], batch_movie_ids))
            if cur.fetchone()[0] < len(batch_movie_ids):
                pending_player = p
                break
    conn.close()

    if not pending_player:
        session[batch_key] = batch_idx + 1
        session.modified = True
        return redirect(url_for("movies_round", session_id=session_id))

    return render_template(
        "movies_round.html",
        session_id=session_id,
        player=pending_player,
        movies=current_batch,
        batch_num=batch_idx + 1,
        approved_count=len(approved_ids),
    )


@app.route("/session/<int:session_id>/movies/vote", methods=["POST"])
def movies_vote(session_id):
    id_player = int(request.form.get("id_player"))
    movie_ids = request.form.getlist("movie_ids")

    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT id_round FROM public.filter_round WHERE id_session = %s ORDER BY round_number DESC LIMIT 1;", (session_id,))
        round_id = cur.fetchone()[0]

        for m_id_str in movie_ids:
            m_id = int(m_id_str)
            vote_val = int(request.form.get(f"vote_{m_id}", 0))
            cur.execute("""
                INSERT INTO public.movie_choice (id_session, id_round, id_player, id_movie, vote_type)
                VALUES (%s, %s, %s, %s, %s);
            """, (session_id, round_id, id_player, m_id, vote_val))

    conn.commit()
    conn.close()
    return redirect(url_for("movies_round", session_id=session_id))


def finalize_top3_movies(session_id, round_id, top3_ids):
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM public.final_top3_movie WHERE id_session = %s;", (session_id,))
        for m_id in top3_ids:
            cur.execute("INSERT INTO public.final_top3_movie (id_session, id_movie) VALUES (%s, %s);", (session_id, m_id))
        cur.execute("UPDATE public.game_session SET status = 'TOP3_VOTE' WHERE id_session = %s;", (session_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("top3_voting", session_id=session_id))


def finalize_top3_fallback(session_id, round_id, all_matching):
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id_movie
            FROM public.movie_choice
            WHERE id_session = %s AND id_round = %s
            GROUP BY id_movie
            ORDER BY SUM(CASE WHEN vote_type = 1 THEN 1 ELSE 0 END) DESC,
                     SUM(CASE WHEN vote_type = 2 THEN 1 ELSE 0 END) ASC
            LIMIT 3;
        """, (session_id, round_id))
        top_ids = [r[0] for r in cur.fetchall()]
    conn.close()

    if len(top_ids) < 3:
        for m in all_matching:
            if m[0] not in top_ids:
                top_ids.append(m[0])
            if len(top_ids) == 3:
                break

    return finalize_top3_movies(session_id, round_id, top_ids[:3])


# =========================================================
# ETAPA 6: VOTAÇÃO NOS 3 FINALISTAS, PEDRA-PAPEL-TESOURA E VENCEDOR
# =========================================================
@app.route("/session/<int:session_id>/top3", methods=["GET", "POST"])
def top3_voting(session_id):
    conn = get_connection()
    with conn.cursor() as cur:
        if request.method == "POST":
            id_player = int(request.form.get("id_player"))
            id_movie = int(request.form.get("id_movie"))
            cur.execute("""
                INSERT INTO public.movie_final_choice (id_session, id_player, id_movie)
                VALUES (%s, %s, %s);
            """, (session_id, id_player, id_movie))
            conn.commit()

        cur.execute("""
            SELECT sp.id_player, sp.player_order, sp.player_name
            FROM public.session_player sp
            WHERE sp.id_session = %s
              AND sp.id_player NOT IN (
                  SELECT id_player FROM public.movie_final_choice WHERE id_session = %s
              )
            ORDER BY sp.player_order ASC;
        """, (session_id, session_id))
        pending_players = cur.fetchall()

        cur.execute("SELECT COUNT(*) FROM public.session_player WHERE id_session = %s;", (session_id,))
        total_players = cur.fetchone()[0]

        cur.execute("""
            SELECT m.id_movie, m.original_title, m.year, m.duration, m.rating, m.genres, m.poster_url
            FROM public.final_top3_movie f
            JOIN public.movie m ON m.id_movie = f.id_movie
            WHERE f.id_session = %s;
        """, (session_id,))
        finalists = cur.fetchall()

    conn.close()

    if pending_players:
        return render_template(
            "top3_vote.html",
            session_id=session_id,
            player=pending_players[0],
            total_players=total_players,
            finalists=finalists,
        )

    return redirect(url_for("evaluate_top3_votes", session_id=session_id))


@app.route("/session/<int:session_id>/top3/evaluate")
def evaluate_top3_votes(session_id):
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT mfc.id_player, sp.player_name, mfc.id_movie
            FROM public.movie_final_choice mfc
            JOIN public.session_player sp ON sp.id_player = mfc.id_player
            WHERE mfc.id_session = %s
            ORDER BY sp.player_order ASC;
        """, (session_id,))
        votes = cur.fetchall()

        chosen_movies = {v[2] for v in votes}

        # Se todos votaram no mesmo filme, vence por unanimidade!
        if len(chosen_movies) == 1:
            winning_movie_id = list(chosen_movies)[0]
            cur.execute("""
                UPDATE public.game_session
                SET status = 'FINISHED', finished_at = CURRENT_TIMESTAMP, winning_movie_id = %s
                WHERE id_session = %s;
            """, (winning_movie_id, session_id))
            conn.commit()
            conn.close()
            session[f"win_reason_{session_id}"] = "Escolhido por Unanimidade pelo Grupo"
            return redirect(url_for("winner_screen", session_id=session_id))

        # Se houve divergência, prepara o Torneio de Pedra-Papel-Tesoura
        cur.execute("UPDATE public.game_session SET status = 'TIE_FINAL' WHERE id_session = %s;", (session_id,))
        conn.commit()
    conn.close()

    session[f"rps_queue_{session_id}"] = [[v[0], v[1]] for v in votes]
    return redirect(url_for("rps_arena", session_id=session_id))


@app.route("/session/<int:session_id>/rps")
def rps_arena(session_id):
    queue_key = f"rps_queue_{session_id}"
    if queue_key not in session:
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT id_player, player_name FROM public.session_player WHERE id_session = %s ORDER BY player_order ASC;", (session_id,))
            players = cur.fetchall()
        conn.close()
        session[queue_key] = [[p[0], p[1]] for p in players]

    queue = session[queue_key]

    # Quando resta apenas 1 jogador, ele é o Campeão do Pedra-Papel-Tesoura!
    if len(queue) == 1:
        champion_id, champion_name = queue[0]
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id_movie FROM public.movie_final_choice
                WHERE id_session = %s AND id_player = %s LIMIT 1;
            """, (session_id, champion_id))
            winning_movie_id = cur.fetchone()[0]

            cur.execute("""
                UPDATE public.game_session
                SET status = 'FINISHED', finished_at = CURRENT_TIMESTAMP, winning_movie_id = %s
                WHERE id_session = %s;
            """, (winning_movie_id, session_id))
        conn.commit()
        conn.close()

        session[f"win_reason_{session_id}"] = f"Definido por {champion_name} no Torneio de Pedra-Papel-Tesoura"
        return redirect(url_for("winner_screen", session_id=session_id))

    p1, p2 = queue[0], queue[1]
    match_title = "Grande Final" if len(queue) == 2 else f"Meia-Final ({len(queue)} Jogadores)"
    return render_template("rps_arena.html", session_id=session_id, p1=p1, p2=p2, match_title=match_title)


@app.route("/session/<int:session_id>/rps/record", methods=["POST"])
def rps_record(session_id):
    p1_id = int(request.form.get("p1_id"))
    p2_id = int(request.form.get("p2_id"))
    winner_id = int(request.form.get("winner_id"))

    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO public.tiebreaker_match (
                id_session, game_type, stage,
                player1_id, player2_id, winner_player_id
            ) VALUES (%s, 'ROCK_PAPER_SCISSORS', 'FINAL_MOVIE_CONFLICT', %s, %s, %s);
        """, (session_id, p1_id, p2_id, winner_id))
    conn.commit()
    conn.close()

    queue_key = f"rps_queue_{session_id}"
    queue = session.get(queue_key, [])
    if len(queue) >= 2:
        p1, p2 = queue[0], queue[1]
        winner_obj = p1 if p1[0] == winner_id else p2
        session[queue_key] = queue[2:] + [winner_obj]
        session.modified = True

    return redirect(url_for("rps_arena", session_id=session_id))


@app.route("/session/<int:session_id>/winner")
def winner_screen(session_id):
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT winning_movie_id FROM public.game_session WHERE id_session = %s;", (session_id,))
        row = cur.fetchone()
        if not row or not row[0]:
            conn.close()
            return redirect(url_for("index"))
        winning_movie_id = row[0]

        cur.execute("""
            SELECT id_movie, current_rank, original_title, year, certificate,
                   duration, rating, vote_count, metascore, directors,
                   writers, stars, genres, languages, countries, link, poster_url
            FROM public.movie
            WHERE id_movie = %s;
        """, (winning_movie_id,))
        movie = cur.fetchone()
    conn.close()

    reason_text = session.get(f"win_reason_{session_id}", "Filme Vencedor da Partida")
    return render_template("winner_movie.html", movie=movie, reason_text=reason_text)


if __name__ == "__main__":
    app.run(debug=True, port=5000)