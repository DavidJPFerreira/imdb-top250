import hmac
import os
import random
import time
from collections import Counter
from flask import Flask, redirect, render_template, render_template_string, request, session, url_for
from translations import GENRE_MAP, TRANSLATIONS, translate_genres_str
from db import (
    ALL_DURATION_CODES,
    ensure_schema_updates,
    filter_movies_in_db,
    get_db_connection,
    get_dynamic_filter_options,
)

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "chave_secreta_imdb_game_2026")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

ensure_schema_updates()

# Proteção contra Brute-Force no Login de Admin
_ADMIN_FAILED_ATTEMPTS = {}
MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_SECONDS = 60


@app.after_request
def add_security_and_no_cache_headers(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    return response


@app.context_processor
def inject_translations():
    lang = session.get("lang", "pt")
    return {
        "t": TRANSLATIONS.get(lang, TRANSLATIONS["pt"]),
        "lang": lang,
        "tr_genres": translate_genres_str,
    }


def get_plot_column(lang):
    return {
        "pt": "COALESCE(NULLIF(TRIM(m.plot_pt), ''), NULLIF(TRIM(m.plot_en), ''), m.plot)",
        "es": "COALESCE(NULLIF(TRIM(m.plot_es), ''), NULLIF(TRIM(m.plot_en), ''), m.plot)",
    }.get(lang, "COALESCE(NULLIF(TRIM(m.plot_en), ''), m.plot)")


@app.route("/set_lang/<lang_code>")
def set_language(lang_code):
    if lang_code in ("pt", "en", "es"):
        session["lang"] = lang_code
    return redirect(request.referrer or url_for("index"))


@app.route("/")
def index():
    with get_db_connection() as conn:
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
    return render_template("landing.html", top_movies=top_movies, total_movies=total_movies)


@app.route("/setup")
def setup_page():
    return render_template("index.html")


@app.route("/start_game", methods=["POST"])
def start_game():
    try:
        num_players = int(request.form.get("num_players", 2))
    except ValueError:
        num_players = 2
    num_players = max(2, min(4, num_players))

    player_names = [
        request.form.get(f"player_{i}", "").strip()[:50]
        for i in range(1, num_players + 1)
        if request.form.get(f"player_{i}", "").strip()
    ]

    if len(player_names) < 2:
        return redirect(url_for("index"))

    with get_db_connection() as conn:
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

    return redirect(url_for("session_filters", session_id=session_id))


@app.route("/session/<int:session_id>/filters", methods=["GET", "POST"])
def session_filters(session_id):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id_round FROM public.filter_round
                WHERE id_session = %s ORDER BY round_number DESC LIMIT 1;
            """, (session_id,))
            round_row = cur.fetchone()
            if not round_row:
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
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id_round, id_player) DO UPDATE SET
                        year_choice = EXCLUDED.year_choice,
                        certificate_choice = EXCLUDED.certificate_choice,
                        duration_choice = EXCLUDED.duration_choice,
                        rating_choice = EXCLUDED.rating_choice,
                        genres_choice = EXCLUDED.genres_choice;
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

    if pending_players:
        lang = session.get("lang", "pt")
        decades, certificates, raw_genres = get_dynamic_filter_options()
        genre_map = GENRE_MAP.get(lang, {})
        genres_localized = [(g, genre_map.get(g, g)) for g in raw_genres]

        return render_template(
            "filters.html",
            session_id=session_id,
            player=pending_players[0],
            total_players=total_players,
            decades=decades,
            certificates=certificates,
            genres=genres_localized,
        )

    return redirect(url_for("evaluate_filters", session_id=session_id))


def compute_merged_filters(choices, winning_category=None, winner_choice=None):
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

    if winning_category == "certificate" and winner_choice:
        final_cert = winner_choice["certificate"]
    else:
        if any(c["certificate"] == "ANY" for c in choices):
            final_cert = "ANY"
        else:
            final_cert = ",".join(sorted({c["certificate"] for c in choices}))

    if winning_category == "rating" and winner_choice:
        final_rating = float(winner_choice["rating"])
    else:
        final_rating = min(float(c["rating"]) for c in choices)

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
    lang = session.get("lang", "pt")
    all_decades, _, all_genres = get_dynamic_filter_options()
    all_durations = sorted(ALL_DURATION_CODES)

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id_round FROM public.filter_round
                WHERE id_session = %s ORDER BY round_number DESC LIMIT 1;
            """, (session_id,))
            round_row = cur.fetchone()
            if not round_row:
                return redirect(url_for("index"))
            round_id = round_row[0]

            cur.execute("""
                SELECT sp.id_player, sp.player_name, fc.year_choice, fc.duration_choice,
                       fc.certificate_choice, fc.rating_choice, fc.genres_choice
                FROM public.filter_choice fc
                JOIN public.session_player sp ON sp.id_player = fc.id_player
                WHERE fc.id_round = %s
                ORDER BY sp.player_order ASC;
            """, (round_id,))
            rows = cur.fetchall()

            if not rows:
                return redirect(url_for("session_filters", session_id=session_id))

            if all(r[2] == "ANY" for r in rows):
                strict_decades = ["ANY"]
            else:
                decade_sets = [set(all_decades) if r[2] == "ANY" else set(r[2].split(",")) for r in rows]
                common_decades = set.intersection(*decade_sets) if decade_sets else set()
                strict_decades = list(common_decades) if common_decades else ["__NONE__"]

            if all(r[3] == "ANY" for r in rows):
                strict_durations = ["ANY"]
            else:
                dur_sets = [set(all_durations) if r[3] == "ANY" else set(r[3].split(",")) for r in rows]
                common_durations = set.intersection(*dur_sets) if dur_sets else set()
                strict_durations = list(common_durations) if common_durations else ["__NONE__"]

            if all(r[6] == "ANY" for r in rows):
                strict_genres = ["ANY"]
            else:
                genre_sets = [set(all_genres) if r[6] == "ANY" else set(r[6].split(",")) for r in rows]
                common_genres = set.intersection(*genre_sets) if genre_sets else set()
                strict_genres = list(common_genres) if common_genres else ["__NONE__"]

            cert_set = {r[4] for r in rows if r[4] and r[4] != "ANY"}
            if not cert_set:
                strict_certs = ["ANY"]
            elif len(cert_set) == 1:
                strict_certs = list(cert_set)
            else:
                strict_certs = ["__NONE__"]

            max_rating = max(float(r[5]) for r in rows)

            matching_strict = filter_movies_in_db(
                strict_decades,
                strict_certs,
                strict_durations,
                max_rating,
                strict_genres,
                lang=lang,
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

    return render_template(
        "filter_summary.html",
        session_id=session_id,
        choices=rows,
        had_conflict=had_conflict,
        matching_count=len(matching_strict),
    )


@app.route("/session/<int:session_id>/tictactoe/start", methods=["POST"])
def tictactoe_start(session_id):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id_round FROM public.filter_round WHERE id_session = %s ORDER BY round_number DESC LIMIT 1;", (session_id,))
            round_id = cur.fetchone()[0]

            cur.execute("SELECT id_player, player_name FROM public.session_player WHERE id_session = %s ORDER BY player_order ASC;", (session_id,))
            players = cur.fetchall()

            used_stakes = set()
            all_cats = ["genres", "decades", "duration", "rating", "certificate"]

            for p_id, _ in players:
                stake = request.form.get(f"stake_{p_id}", "genres")
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

    session[f"ttt_queue_{session_id}"] = [[p[0], p[1]] for p in players]
    return redirect(url_for("tictactoe_arena", session_id=session_id))


@app.route("/session/<int:session_id>/tictactoe")
def tictactoe_arena(session_id):
    queue_key = f"ttt_queue_{session_id}"
    if queue_key not in session:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id_player, player_name FROM public.session_player WHERE id_session = %s ORDER BY player_order ASC;", (session_id,))
                players = cur.fetchall()
        session[queue_key] = [[p[0], p[1]] for p in players]

    queue = session[queue_key]
    if len(queue) == 1:
        return redirect(url_for("apply_winner_filter", session_id=session_id, winner_id=queue[0][0]))

    lang = session.get("lang", "pt")
    t = TRANSLATIONS.get(lang, TRANSLATIONS["pt"])
    p1, p2 = queue[0], queue[1]
    match_title = t["ttt_final"] if len(queue) == 2 else f"{t['ttt_elim']} ({len(queue)} {t['player_label']}s)"
    return render_template("tictactoe.html", session_id=session_id, p1=p1, p2=p2, match_title=match_title)


@app.route("/session/<int:session_id>/tictactoe/record", methods=["POST"])
def tictactoe_record(session_id):
    p1_id = int(request.form.get("p1_id"))
    p2_id = int(request.form.get("p2_id"))
    winner_id = int(request.form.get("winner_id"))

    with get_db_connection() as conn:
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
    with get_db_connection() as conn:
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

    session.pop(f"batch_idx_{session_id}", None)

    lang = session.get("lang", "pt")
    t = TRANSLATIONS.get(lang, TRANSLATIONS["pt"])
    cat_names = {
        "genres": t["col_gen"],
        "decades": t["col_year"],
        "duration": t["col_dur"],
        "rating": t["col_rat"],
        "certificate": t["col_age"],
    }
    won_cat_label = cat_names.get(winning_category, winning_category)

    return render_template(
        "ttt_winner.html",
        winner_name=winner_name,
        won_cat_label=won_cat_label,
        merged=merged,
        session_id=session_id,
    )


def expand_adjacent_decades(dec_list):
    if not dec_list or "ANY" in dec_list:
        return ["ANY"]
    expanded = set(dec_list)
    for d in dec_list:
        try:
            yr = int(d.replace("s", ""))
            expanded.add(f"{yr - 10}s")
            expanded.add(f"{yr + 10}s")
        except ValueError:
            pass
    return sorted(expanded)


def get_session_movie_pool(session_id, round_id, min_needed=9, lang="pt"):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT applied_decades, applied_certificate, applied_duration_code,
                       applied_min_rating, applied_genre
                FROM public.filter_round WHERE id_round = %s;
            """, (round_id,))
            row = cur.fetchone()

    dec_list = row[0].split(",") if row[0] and row[0] != "ANY" else ["ANY"]
    cert_list = row[1].split(",") if row[1] and row[1] != "ANY" else ["ANY"]
    dur_list = row[2].split(",") if row[2] and row[2] != "ANY" else ["ANY"]
    min_rat = float(row[3]) if row[3] else 8.0
    gen_list = row[4].split(",") if row[4] and row[4] != "ANY" else ["ANY"]

    adj_decades = expand_adjacent_decades(dec_list)

    relaxation_steps = [
        (dec_list, cert_list, dur_list, min_rat, gen_list),
        (dec_list, cert_list, ["ANY"], min_rat, gen_list),
        (dec_list, cert_list, ["ANY"], max(8.0, min_rat - 0.3), gen_list),
        (dec_list, cert_list, ["ANY"], 8.0, gen_list),
        (adj_decades, cert_list, ["ANY"], 8.0, gen_list),
        (["ANY"], cert_list, ["ANY"], 8.0, gen_list),
        (dec_list, cert_list, ["ANY"], 8.0, ["ANY"]),
        (adj_decades, cert_list, ["ANY"], 8.0, ["ANY"]),
        (["ANY"], cert_list, ["ANY"], 8.0, ["ANY"]),
        (["ANY"], ["ANY"], ["ANY"], 8.0, ["ANY"]),
    ]

    collected = []
    seen_ids = set()

    for d_step, c_step, dur_step, r_step, g_step in relaxation_steps:
        step_movies = filter_movies_in_db(d_step, c_step, dur_step, r_step, g_step, lang=lang)
        for m in step_movies:
            if m[0] not in seen_ids:
                seen_ids.add(m[0])
                collected.append(m)
        if len(collected) >= max(min_needed, 15):
            break

    return collected


def get_player_round_movies(session_id, pool_movies, round_idx, player_order, num_players):
    block_num = round_idx // 3
    step_in_block = round_idx % 3

    block_start = block_num * 9
    block_movies = pool_movies[block_start : block_start + 9]

    if not block_movies:
        return []

    if len(block_movies) == 9:
        sub_batches = [block_movies[0:3], block_movies[3:6], block_movies[6:9]]
        shifted_idx = (step_in_block + (player_order - 1)) % 3
        chosen_batch = list(sub_batches[shifted_idx])
        rng = random.Random(f"{session_id}_{round_idx}_{player_order}")
        rng.shuffle(chosen_batch)
        return chosen_batch

    start = step_in_block * 3
    sub = list(block_movies[start : start + 3])
    rng = random.Random(f"{session_id}_{round_idx}_{player_order}")
    rng.shuffle(sub)
    return sub


@app.route("/session/<int:session_id>/movies")
def movies_round(session_id):
    lang = session.get("lang", "pt")
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id_round FROM public.filter_round WHERE id_session = %s ORDER BY round_number DESC LIMIT 1;", (session_id,))
            round_row = cur.fetchone()
            if not round_row:
                return redirect(url_for("index"))
            round_id = round_row[0]

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

    batch_key = f"batch_idx_{session_id}"
    batch_idx = session.get(batch_key, 0)

    if batch_idx >= 3 and len(approved_ids) >= 3:
        return finalize_top3_movies(session_id, round_id, approved_ids[:3])

    all_matching = get_session_movie_pool(
        session_id, round_id, min_needed=(batch_idx + 3) * 3, lang=lang
    )

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            pending_player = None
            player_movies = []

            for p in players:
                p_id, p_order, p_name = p
                candidate_movies = get_player_round_movies(session_id, all_matching, batch_idx, p_order, num_players)
                if not candidate_movies:
                    continue

                cand_ids = [m[0] for m in candidate_movies]
                cur.execute("""
                    SELECT COUNT(DISTINCT id_movie) FROM public.movie_choice
                    WHERE id_session = %s AND id_round = %s AND id_player = %s
                      AND id_movie = ANY(%s);
                """, (session_id, round_id, p_id, cand_ids))
                voted_count = cur.fetchone()[0]

                if voted_count < len(cand_ids):
                    pending_player = p
                    player_movies = candidate_movies
                    break

    if not pending_player and not get_player_round_movies(session_id, all_matching, batch_idx, 1, num_players):
        if len(approved_ids) >= 3:
            return finalize_top3_movies(session_id, round_id, approved_ids[:3])
        return finalize_top3_fallback(session_id, round_id, all_matching)

    if not pending_player:
        session[batch_key] = batch_idx + 1
        session.modified = True
        return redirect(url_for("movies_round", session_id=session_id))

    return render_template(
        "movies_round.html",
        session_id=session_id,
        player=pending_player,
        movies=player_movies,
        batch_num=batch_idx + 1,
        approved_count=len(approved_ids),
    )


@app.route("/session/<int:session_id>/movies/vote", methods=["POST"])
def movies_vote(session_id):
    id_player = int(request.form.get("id_player"))
    movie_ids = request.form.getlist("movie_ids")

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id_round FROM public.filter_round WHERE id_session = %s ORDER BY round_number DESC LIMIT 1;", (session_id,))
            round_id = cur.fetchone()[0]

            for m_id_str in movie_ids:
                m_id = int(m_id_str)
                vote_val = int(request.form.get(f"vote_{m_id}", 0))
                cur.execute("""
                    INSERT INTO public.movie_choice (id_session, id_round, id_player, id_movie, vote_type)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (id_session, id_round, id_player, id_movie) DO UPDATE SET
                        vote_type = EXCLUDED.vote_type;
                """, (session_id, round_id, id_player, m_id, vote_val))
        conn.commit()

    return redirect(url_for("movies_round", session_id=session_id))


def finalize_top3_movies(session_id, round_id, top3_ids):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM public.final_top3_movie WHERE id_session = %s;", (session_id,))
            for m_id in top3_ids:
                cur.execute("INSERT INTO public.final_top3_movie (id_session, id_movie) VALUES (%s, %s);", (session_id, m_id))
            cur.execute("UPDATE public.game_session SET status = 'TOP3_VOTE' WHERE id_session = %s;", (session_id,))
        conn.commit()
    return redirect(url_for("top3_voting", session_id=session_id))


def finalize_top3_fallback(session_id, round_id, all_matching):
    with get_db_connection() as conn:
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

    if len(top_ids) < 3:
        for m in all_matching:
            if m[0] not in top_ids:
                top_ids.append(m[0])
            if len(top_ids) == 3:
                break

    return finalize_top3_movies(session_id, round_id, top_ids[:3])


@app.route("/session/<int:session_id>/top3", methods=["GET", "POST"])
def top3_voting(session_id):
    lang = session.get("lang", "pt")
    plot_expr = get_plot_column(lang)

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            if request.method == "POST":
                id_player = int(request.form.get("id_player"))
                id_movie = int(request.form.get("id_movie"))
                cur.execute("""
                    INSERT INTO public.movie_final_choice (id_session, id_player, id_movie)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (id_session, id_player) DO UPDATE SET
                        id_movie = EXCLUDED.id_movie;
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

            cur.execute(f"""
                SELECT m.id_movie, m.original_title, m.year, m.duration, m.rating,
                       m.genres, m.poster_url, {plot_expr} AS plot
                FROM public.final_top3_movie f
                JOIN public.movie m ON m.id_movie = f.id_movie
                WHERE f.id_session = %s;
            """, (session_id,))
            finalists = cur.fetchall()

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
    with get_db_connection() as conn:
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

            if len(chosen_movies) == 1:
                winning_movie_id = list(chosen_movies)[0]
                cur.execute("""
                    UPDATE public.game_session
                    SET status = 'FINISHED', finished_at = CURRENT_TIMESTAMP, winning_movie_id = %s
                    WHERE id_session = %s;
                """, (winning_movie_id, session_id))
                conn.commit()
                session[f"win_type_{session_id}"] = "UNANIMOUS"
                session.pop(f"win_champ_{session_id}", None)
                return redirect(url_for("winner_screen", session_id=session_id))

            cur.execute("UPDATE public.game_session SET status = 'TIE_FINAL' WHERE id_session = %s;", (session_id,))
            conn.commit()

    session[f"rps_queue_{session_id}"] = [[v[0], v[1]] for v in votes]
    return redirect(url_for("rps_arena", session_id=session_id))


@app.route("/session/<int:session_id>/rps")
def rps_arena(session_id):
    queue_key = f"rps_queue_{session_id}"
    if queue_key not in session:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id_player, player_name FROM public.session_player WHERE id_session = %s ORDER BY player_order ASC;", (session_id,))
                players = cur.fetchall()
        session[queue_key] = [[p[0], p[1]] for p in players]

    queue = session[queue_key]

    if len(queue) == 1:
        champion_id, champion_name = queue[0]
        with get_db_connection() as conn:
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

        session[f"win_type_{session_id}"] = "RPS"
        session[f"win_champ_{session_id}"] = champion_name
        return redirect(url_for("winner_screen", session_id=session_id))

    lang = session.get("lang", "pt")
    t = TRANSLATIONS.get(lang, TRANSLATIONS["pt"])
    p1, p2 = queue[0], queue[1]
    match_title = t["ttt_final"] if len(queue) == 2 else f"{t['ttt_elim']} ({len(queue)} {t['player_label']}s)"
    return render_template("rps_arena.html", session_id=session_id, p1=p1, p2=p2, match_title=match_title)


@app.route("/session/<int:session_id>/rps/record", methods=["POST"])
def rps_record(session_id):
    p1_id = int(request.form.get("p1_id"))
    p2_id = int(request.form.get("p2_id"))
    winner_id = int(request.form.get("winner_id"))

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO public.tiebreaker_match (
                    id_session, game_type, stage,
                    player1_id, player2_id, winner_player_id
                ) VALUES (%s, 'ROCK_PAPER_SCISSORS', 'FINAL_MOVIE_CONFLICT', %s, %s, %s);
            """, (session_id, p1_id, p2_id, winner_id))
        conn.commit()

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
    lang = session.get("lang", "pt")
    t = TRANSLATIONS.get(lang, TRANSLATIONS["pt"])
    plot_expr = get_plot_column(lang)

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT winning_movie_id FROM public.game_session WHERE id_session = %s;", (session_id,))
            row = cur.fetchone()
            if not row or not row[0]:
                return redirect(url_for("index"))
            winning_movie_id = row[0]

            cur.execute(f"""
                SELECT m.id_movie, m.current_rank, m.original_title, m.year, m.certificate,
                       m.duration, m.rating, m.vote_count, m.metascore, m.directors,
                       m.writers, m.stars, m.genres, m.languages, m.countries, m.link,
                       m.poster_url, {plot_expr} AS translated_plot
                FROM public.movie m
                WHERE m.id_movie = %s;
            """, (winning_movie_id,))
            movie = cur.fetchone()

    win_type = session.get(f"win_type_{session_id}", "UNANIMOUS")
    if win_type == "RPS":
        champ = session.get(f"win_champ_{session_id}", "")
        reason_text = f"{t['win_by_rps']} {champ}".strip()
    else:
        reason_text = t["win_unanimous"]

    movie_plot = movie[17] if movie and len(movie) > 17 else ""
    return render_template(
        "winner_movie.html",
        movie=movie,
        movie_plot=movie_plot,
        reason_text=reason_text,
    )


# =========================================================
# ETAPA 7: DASHBOARD DE ADMIN (/admin)
# =========================================================
def counter_to_bars(counter_obj, limit=12):
    items = counter_obj.most_common(limit)
    if not items:
        return []
    max_val = max(c for _, c in items) or 1
    return [(k, c, int((c / max_val) * 100)) for k, c in items]


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = None
    client_ip = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()
    now = time.time()

    attempts = [ts for ts in _ADMIN_FAILED_ATTEMPTS.get(client_ip, []) if now - ts < LOCKOUT_SECONDS]
    _ADMIN_FAILED_ATTEMPTS[client_ip] = attempts

    if len(attempts) >= MAX_LOGIN_ATTEMPTS:
        wait_sec = int(LOCKOUT_SECONDS - (now - attempts[0]))
        return render_template(
            "admin_login.html",
            error=f"Demasiadas tentativas falhadas. Aguarda {wait_sec}s antes de tentar novamente.",
        )

    if request.method == "POST":
        entered_pw = request.form.get("password", "")
        expected_pw = os.getenv("ADMIN_PASSWORD", "admin123").strip()
        if hmac.compare_digest(entered_pw, expected_pw):
            _ADMIN_FAILED_ATTEMPTS.pop(client_ip, None)
            session["is_admin_authenticated"] = True
            return redirect(url_for("admin_dashboard"))

        attempts.append(now)
        _ADMIN_FAILED_ATTEMPTS[client_ip] = attempts
        error = "Password incorreta!"

    return render_template("admin_login.html", error=error)


@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin_authenticated", None)
    return redirect(url_for("admin_login"))


@app.route("/admin")
def admin_dashboard():
    if not session.get("is_admin_authenticated"):
        return redirect(url_for("admin_login"))

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM public.game_session;")
            total_sessions = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM public.game_session WHERE status = 'FINISHED';")
            finished_sessions = cur.fetchone()[0]

            completion_rate = int(round((finished_sessions / total_sessions) * 100)) if total_sessions > 0 else 0

            cur.execute("SELECT COUNT(*) FROM public.session_player;")
            total_players = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM public.filter_round;")
            total_rounds = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM public.filter_round WHERE had_conflict = TRUE;")
            conflict_rounds = cur.fetchone()[0]

            conflict_rate = int(round((conflict_rounds / total_rounds) * 100)) if total_rounds > 0 else 0

            cur.execute("SELECT num_players, COUNT(*) FROM public.game_session GROUP BY num_players;")
            by_p = dict(cur.fetchall())
            sessions_2p = by_p.get(2, 0)
            sessions_3p = by_p.get(3, 0)
            sessions_4p = by_p.get(4, 0)

            cur.execute("SELECT COUNT(*) FROM public.movie_choice;")
            total_movie_votes = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM public.tiebreaker_match WHERE game_type = 'TIC_TAC_TOE';")
            ttt_matches = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM public.tiebreaker_match WHERE game_type = 'ROCK_PAPER_SCISSORS';")
            rps_matches = cur.fetchone()[0]

            cur.execute("""
                SELECT year_choice, duration_choice, certificate_choice,
                       rating_choice, genres_choice, combat_category
                FROM public.filter_choice;
            """)

            gen_decades = Counter()
            gen_durations = Counter()
            gen_certs = Counter()
            gen_ratings = Counter()
            gen_genres = Counter()

            combat_cats_count = {
                "genres": 0,
                "decades": 0,
                "duration": 0,
                "certificate": 0,
                "rating": 0,
            }
            combat_spec = {
                "genres": Counter(),
                "decades": Counter(),
                "duration": Counter(),
                "certificate": Counter(),
                "rating": Counter(),
            }

            dur_normalizer = {"=2h": "1h30-2h", "=2h30": "2h-2h30"}

            for yr_c, dur_c, cert_c, rat_c, gen_c, comb_c in cur.fetchall():
                dec_vals = [d.strip() for d in (yr_c or "ANY").split(",") if d.strip()]
                for d in dec_vals:
                    gen_decades["Todas (Sem filtro)" if d == "ANY" else d] += 1

                dur_vals = [dur_normalizer.get(d.strip(), d.strip()) for d in (dur_c or "ANY").split(",") if d.strip()]
                for d in dur_vals:
                    gen_durations["Todas (Sem filtro)" if d == "ANY" else d] += 1

                cert_val = "Qualquer Idade" if (not cert_c or cert_c == "ANY") else cert_c.strip()
                gen_certs[cert_val] += 1

                rat_val = str(rat_c) if rat_c is not None else "8.0"
                gen_ratings[rat_val] += 1

                gen_vals = [g.strip() for g in (gen_c or "ANY").split(",") if g.strip()]
                for g in gen_vals:
                    gen_genres["Todos (Sem filtro)" if g == "ANY" else translate_genres_str(g, "pt")] += 1

                if comb_c in combat_cats_count:
                    combat_cats_count[comb_c] += 1
                    if comb_c == "genres":
                        for g in gen_vals:
                            combat_spec["genres"]["Todos" if g == "ANY" else translate_genres_str(g, "pt")] += 1
                    elif comb_c == "decades":
                        for d in dec_vals:
                            combat_spec["decades"]["Todas" if d == "ANY" else d] += 1
                    elif comb_c == "duration":
                        for d in dur_vals:
                            combat_spec["duration"]["Todas" if d == "ANY" else d] += 1
                    elif comb_c == "certificate":
                        combat_spec["certificate"][cert_val] += 1
                    elif comb_c == "rating":
                        combat_spec["rating"][rat_val] += 1

            cat_labels = [
                ("genres", "Género"),
                ("decades", "Ano (Décadas)"),
                ("duration", "Duração"),
                ("certificate", "Restrição de Idade"),
                ("rating", "Pontuação IMDb"),
            ]
            max_comb = max(combat_cats_count.values()) or 1
            combat_overview = [
                (label, combat_cats_count[key], int((combat_cats_count[key] / max_comb) * 100))
                for key, label in cat_labels
            ]
            combat_overview.sort(key=lambda x: x[1], reverse=True)

            cur.execute("""
                SELECT m.original_title, COUNT(*) AS cnt
                FROM public.game_session gs
                JOIN public.movie m ON m.id_movie = gs.winning_movie_id
                GROUP BY m.original_title ORDER BY cnt DESC LIMIT 6;
            """)
            winning_movies = cur.fetchall()

            cur.execute("""
                SELECT m.original_title, COUNT(*) AS cnt
                FROM public.movie_choice mc JOIN public.movie m ON m.id_movie = mc.id_movie
                WHERE mc.vote_type = 1 GROUP BY m.original_title ORDER BY cnt DESC LIMIT 6;
            """)
            most_liked = cur.fetchall()

            cur.execute("""
                SELECT m.original_title, COUNT(*) AS cnt
                FROM public.movie_choice mc JOIN public.movie m ON m.id_movie = mc.id_movie
                WHERE mc.vote_type = 2 GROUP BY m.original_title ORDER BY cnt DESC LIMIT 6;
            """)
            most_seen = cur.fetchall()

            cur.execute("""
                SELECT m.original_title, COUNT(*) AS cnt
                FROM public.movie_choice mc JOIN public.movie m ON m.id_movie = mc.id_movie
                WHERE mc.vote_type = 0 GROUP BY m.original_title ORDER BY cnt DESC LIMIT 6;
            """)
            most_disliked = cur.fetchall()

            cur.execute("""
                SELECT gs.id_session, gs.created_at,
                       STRING_AGG(sp.player_name, ', ' ORDER BY sp.player_order),
                       gs.num_players, gs.status, m.original_title
                FROM public.game_session gs
                LEFT JOIN public.session_player sp ON sp.id_session = gs.id_session
                LEFT JOIN public.movie m ON m.id_movie = gs.winning_movie_id
                GROUP BY gs.id_session, gs.created_at, gs.num_players, gs.status, m.original_title
                ORDER BY gs.id_session DESC LIMIT 10;
            """)
            recent_sessions = cur.fetchall()

    kpis = {
        "total_sessions": total_sessions,
        "finished_sessions": finished_sessions,
        "completion_rate": completion_rate,
        "total_players": total_players,
        "total_movie_votes": total_movie_votes,
        "conflict_rate": conflict_rate,
        "sessions_2p": sessions_2p,
        "sessions_3p": sessions_3p,
        "sessions_4p": sessions_4p,
        "ttt_matches": ttt_matches,
        "rps_matches": rps_matches,
    }

    general_filters = {
        "genres": counter_to_bars(gen_genres),
        "decades": counter_to_bars(gen_decades),
        "durations": counter_to_bars(gen_durations),
        "certificates": counter_to_bars(gen_certs),
        "ratings": counter_to_bars(gen_ratings),
    }

    combat_specific = {
        "genres": counter_to_bars(combat_spec["genres"]),
        "decades": counter_to_bars(combat_spec["decades"]),
        "duration": counter_to_bars(combat_spec["duration"]),
        "certificate": counter_to_bars(combat_spec["certificate"]),
        "rating": counter_to_bars(combat_spec["rating"]),
    }

    return render_template(
        "admin_dashboard.html",
        kpis=kpis,
        general_filters=general_filters,
        combat_overview=combat_overview,
        combat_specific=combat_specific,
        winning_movies=winning_movies,
        most_liked=most_liked,
        most_seen=most_seen,
        most_disliked=most_disliked,
        recent_sessions=recent_sessions,
    )


if __name__ == "__main__":
    is_debug = os.getenv("FLASK_DEBUG", "1") == "1"
    app.run(debug=is_debug, port=int(os.getenv("PORT", "5000")))