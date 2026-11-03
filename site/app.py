import os
from pathlib import Path
import psycopg2
from dotenv import load_dotenv
from flask import Flask, redirect, render_template_string, request, url_for

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env" if (BASE_DIR / ".env").exists() else BASE_DIR.parent / ".env"
load_dotenv(dotenv_path=ENV_PATH)

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "chave_secreta_imdb_game_2026")


def get_connection():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost").strip(),
        port=os.getenv("DB_PORT", "5432").strip(),
        dbname=os.getenv("DB_NAME", "movie_game").strip(),
        user=os.getenv("DB_USER", "postgres").strip(),
        password=os.getenv("DB_PASSWORD", "").strip(),
    )


def ensure_filter_columns():
    """Garante que as colunas de décadas e duração comportam os novos formatos de texto."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("""
            ALTER TABLE public.filter_choice
            ALTER COLUMN year_choice TYPE VARCHAR(100) USING year_choice::VARCHAR,
            ALTER COLUMN duration_choice TYPE VARCHAR(50) USING duration_choice::VARCHAR;

            ALTER TABLE public.filter_round
            ADD COLUMN IF NOT EXISTS applied_decades VARCHAR(100),
            ADD COLUMN IF NOT EXISTS applied_duration_code VARCHAR(50),
            ADD COLUMN IF NOT EXISTS conflict_fields VARCHAR(255);
        """)
    conn.commit()
    conn.close()


ensure_filter_columns()


def get_dynamic_filter_options():
    """Extrai décadas, classificações válidas (sem Not Rated) e géneros da BD."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT (year / 10) * 10 AS decade
            FROM public.movie
            WHERE is_in_top250 = TRUE AND year IS NOT NULL
            ORDER BY decade ASC;
        """)
        decades = [f"{row[0]}s" for row in cur.fetchall()]

        cur.execute("""
            SELECT DISTINCT certificate
            FROM public.movie
            WHERE is_in_top250 = TRUE
              AND certificate IS NOT NULL
              AND TRIM(certificate) != ''
              AND LOWER(certificate) NOT IN ('not rated', 'unrated', 'none', 'nan')
            ORDER BY certificate ASC;
        """)
        certificates = [row[0] for row in cur.fetchall()]

        cur.execute("""
            SELECT genres FROM public.movie WHERE is_in_top250 = TRUE AND genres IS NOT NULL;
        """)
        genre_set = set()
        for (g_str,) in cur.fetchall():
            for g in g_str.split(","):
                g_clean = g.strip()
                if g_clean:
                    genre_set.add(g_clean)
        genres = sorted(genre_set)

    conn.close()
    return decades, certificates, genres


def filter_movies_in_db(decades_list, cert_list, duration_list, min_rating, genres_list):
    """Retorna os filmes do Top 250 que cumprem os filtros combinados."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id_movie, original_title, year, certificate, duration_minutes, rating, genres
            FROM public.movie
            WHERE is_in_top250 = TRUE;
        """)
        all_movies = cur.fetchall()
    conn.close()

    matching = []
    for m in all_movies:
        m_id, title, year, cert, dur_min, rating, m_genres = m

        # 1. Décadas (ex: ['1980s', '1990s'])
        if decades_list and year:
            m_decade = f"{(int(year) // 10) * 10}s"
            if m_decade not in decades_list:
                continue

        # 2. Classificação Etária (ignora se 'ANY' estiver presente)
        if cert_list and "ANY" not in cert_list:
            if not cert or cert not in cert_list:
                continue

        # 3. Duração (<1h30: <=90 | =2h: 105..135 | =2h30: 135..165 | >2h30: >165)
        if duration_list and dur_min is not None:
            dur_ok = False
            for d_code in duration_list:
                if d_code == "<1h30" and dur_min <= 90:
                    dur_ok = True
                elif d_code == "=2h" and 105 <= dur_min <= 135:
                    dur_ok = True
                elif d_code == "=2h30" and 135 <= dur_min <= 165:
                    dur_ok = True
                elif d_code == ">2h30" and dur_min > 165:
                    dur_ok = True
            if not dur_ok:
                continue

        # 4. Rating mínimo
        if min_rating and rating is not None:
            if float(rating) < float(min_rating):
                continue

        # 5. Género
        if genres_list and m_genres:
            movie_g_set = {g.strip() for g in m_genres.split(",")}
            if not movie_g_set.intersection(set(genres_list)):
                continue

        matching.append(m)

    return matching


HOME_HTML = """
<!DOCTYPE html>
<html lang="pt">
<head>
    <meta charset="UTF-8">
    <title>IMDb Top 250 - Movie Picker Game</title>
    <style>
        * { box-sizing: border-box; }
        body {
            background-color: #121212; color: #ffffff;
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            margin: 0; padding: 30px 20px; text-align: center;
        }
        h1 { color: #f5c518; font-size: 2.2rem; margin-bottom: 8px; }
        p.subtitle { color: #b3b3b3; margin-top: 0; margin-bottom: 30px; }
        .setup-box {
            background-color: #1e1e1e; max-width: 520px; margin: 0 auto 50px auto;
            padding: 30px; border-radius: 14px; border: 1px solid #2e2e2e;
            box-shadow: 0 10px 25px rgba(0,0,0,0.7);
        }
        .count-selector { display: flex; justify-content: center; gap: 15px; margin-bottom: 25px; }
        .count-btn {
            background-color: #2c2c2c; color: #fff; border: 2px solid #444;
            padding: 12px 24px; font-size: 1.05rem; font-weight: bold;
            border-radius: 8px; cursor: pointer;
        }
        .count-btn.active { background-color: #f5c518; color: #000; border-color: #f5c518; }
        .player-inputs { display: flex; flex-direction: column; gap: 12px; margin-bottom: 25px; }
        .player-row { display: flex; align-items: center; gap: 12px; }
        .player-badge {
            background-color: #333; color: #f5c518; font-weight: bold;
            padding: 10px 14px; border-radius: 8px; min-width: 110px;
        }
        input[type="text"] {
            flex: 1; padding: 12px 14px; border-radius: 8px;
            border: 1px solid #444; background-color: #141414; color: #fff; font-size: 1rem;
        }
        .start-btn {
            width: 100%; background-color: #f5c518; color: #000; font-weight: bold;
            font-size: 1.15rem; padding: 14px; border: none; border-radius: 8px; cursor: pointer;
        }
        .cards { display: flex; justify-content: center; gap: 20px; flex-wrap: wrap; }
        .card { background-color: #1e1e1e; border-radius: 10px; width: 180px; overflow: hidden; }
        .card img { width: 100%; height: 260px; object-fit: cover; }
        .card h3 { font-size: 0.95rem; padding: 10px; margin: 0; }
    </style>
</head>
<body>
    <h1>🎬 IMDb Top 250 Movie Game</h1>
    <p class="subtitle">Escolham quantos jogadores estão no ecrã e insiram os vossos nomes</p>

    <div class="setup-box">
        <form action="/start_game" method="POST">
            <input type="hidden" name="num_players" id="num_players" value="2">
            <div class="count-selector">
                <button type="button" class="count-btn active" onclick="setPlayers(2)">2 Jogadores</button>
                <button type="button" class="count-btn" onclick="setPlayers(3)">3 Jogadores</button>
                <button type="button" class="count-btn" onclick="setPlayers(4)">4 Jogadores</button>
            </div>
            <div class="player-inputs">
                <div class="player-row" id="row-1">
                    <span class="player-badge">Jogador 1</span>
                    <input type="text" name="player_1" placeholder="Nome do Jogador 1" required>
                </div>
                <div class="player-row" id="row-2">
                    <span class="player-badge">Jogador 2</span>
                    <input type="text" name="player_2" placeholder="Nome do Jogador 2" required>
                </div>
                <div class="player-row" id="row-3" style="display: none;">
                    <span class="player-badge">Jogador 3</span>
                    <input type="text" name="player_3" id="input-3" placeholder="Nome do Jogador 3">
                </div>
                <div class="player-row" id="row-4" style="display: none;">
                    <span class="player-badge">Jogador 4</span>
                    <input type="text" name="player_4" id="input-4" placeholder="Nome do Jogador 4">
                </div>
            </div>
            <button type="submit" class="start-btn">Começar Partida ➔</button>
        </form>
    </div>

    <div>
        <h2 style="color:#ccc; font-size:1.2rem;">Top 3 na Base de Dados ({{ total_movies }} filmes ativos)</h2>
        <div class="cards">
            {% for m in top_movies %}
            <div class="card">
                <img src="{{ m[1] }}" alt="{{ m[0] }}">
                <h3>{{ m[0] }}</h3>
            </div>
            {% endfor %}
        </div>
    </div>

    <script>
        function setPlayers(n) {
            document.getElementById('num_players').value = n;
            document.querySelectorAll('.count-btn').forEach((btn, idx) => {
                btn.classList.toggle('active', (idx + 2) === n);
            });
            for (let i = 3; i <= 4; i++) {
                const row = document.getElementById('row-' + i);
                const inp = document.getElementById('input-' + i);
                row.style.display = (i <= n) ? 'flex' : 'none';
                inp.required = (i <= n);
                if (i > n) inp.value = '';
            }
        }
    </script>
</body>
</html>
"""

FILTER_TURN_HTML = """
<!DOCTYPE html>
<html lang="pt">
<head>
    <meta charset="UTF-8">
    <title>Filtros - Vez de {{ player[2] }}</title>
    <style>
        * { box-sizing: border-box; }
        body {
            background-color: #121212; color: #fff;
            font-family: 'Segoe UI', sans-serif; margin: 0; padding: 30px 20px;
        }
        .container {
            max-width: 720px; margin: 0 auto; background: #1e1e1e;
            padding: 30px; border-radius: 14px; border: 1px solid #333;
        }
        .turn-banner {
            background: #f5c518; color: #000; padding: 12px 20px;
            border-radius: 8px; font-weight: bold; font-size: 1.25rem;
            margin-bottom: 25px; text-align: center;
        }
        .section { margin-bottom: 26px; text-align: left; }
        .section h3 { margin: 0 0 10px 0; color: #f5c518; font-size: 1.05rem; }
        .section small { color: #aaa; font-weight: normal; }
        .chips { display: flex; flex-wrap: wrap; gap: 10px; }
        .chip input { display: none; }
        .chip span {
            display: inline-block; padding: 10px 16px; background: #2b2b2b;
            border: 2px solid #444; border-radius: 8px; cursor: pointer;
            font-weight: 600; transition: all 0.15s; user-select: none;
        }
        .chip input:checked + span {
            background: #f5c518; color: #000; border-color: #f5c518;
        }
        .submit-btn {
            width: 100%; background: #f5c518; color: #000; font-weight: bold;
            font-size: 1.15rem; padding: 15px; border: none; border-radius: 8px;
            cursor: pointer; margin-top: 10px;
        }
        .error-msg {
            color: #ff6b6b; font-weight: bold; margin-top: 8px; display: none;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="turn-banner">
            🎮 Vez de {{ player[2] }} (Jogador {{ player[1] }} de {{ total_players }})
        </div>

        <form action="/session/{{ session_id }}/filters" method="POST" onsubmit="return validateForm()">
            <input type="hidden" name="id_player" value="{{ player[0] }}">

            <!-- 1. DÉCADAS (Máximo 3) -->
            <div class="section">
                <h3>1. Ano do Filme — Décadas <small>(Escolhe entre 1 e 3 opções no máximo)</small></h3>
                <div class="chips">
                    {% for dec in decades %}
                    <label class="chip">
                        <input type="checkbox" name="decades" value="{{ dec }}" onchange="limitDecades(this)">
                        <span>{{ dec }}</span>
                    </label>
                    {% endfor %}
                </div>
                <div id="decade-error" class="error-msg">Seleciona pelo menos 1 década (máximo 3)!</div>
            </div>

            <!-- 2. DURAÇÃO (±15 min) -->
            <div class="section">
                <h3>2. Duração do Filme <small>(Intervalo aproximado ±15 min)</small></h3>
                <div class="chips">
                    <label class="chip">
                        <input type="radio" name="duration" value="<1h30" required>
                        <span>&lt; 1h30 (até 90m)</span>
                    </label>
                    <label class="chip">
                        <input type="radio" name="duration" value="=2h">
                        <span>= 2h (1h45 a 2h15)</span>
                    </label>
                    <label class="chip">
                        <input type="radio" name="duration" value="=2h30">
                        <span>= 2h30 (2h15 a 2h45)</span>
                    </label>
                    <label class="chip">
                        <input type="radio" name="duration" value=">2h30">
                        <span>&gt; 2h30 (+165m)</span>
                    </label>
                </div>
            </div>

            <!-- 3. CLASSIFICAÇÃO ETÁRIA (Sem Not Rated) -->
            <div class="section">
                <h3>3. Restrição de Idade <small>(Apenas classificações definidas)</small></h3>
                <div class="chips">
                    <label class="chip">
                        <input type="radio" name="certificate" value="ANY" checked>
                        <span>Qualquer Idade</span>
                    </label>
                    {% for cert in certificates %}
                    <label class="chip">
                        <input type="radio" name="certificate" value="{{ cert }}">
                        <span>{{ cert }}</span>
                    </label>
                    {% endfor %}
                </div>
            </div>

            <!-- 4. RATING MÍNIMO -->
            <div class="section">
                <h3>4. Pontuação Mínima no IMDb</h3>
                <div class="chips">
                    <label class="chip">
                        <input type="radio" name="rating" value="8.0" checked>
                        <span>⭐ 8.0+</span>
                    </label>
                    <label class="chip">
                        <input type="radio" name="rating" value="8.3">
                        <span>⭐ 8.3+</span>
                    </label>
                    <label class="chip">
                        <input type="radio" name="rating" value="8.5">
                        <span>⭐ 8.5+</span>
                    </label>
                    <label class="chip">
                        <input type="radio" name="rating" value="8.8">
                        <span>⭐ 8.8+</span>
                    </label>
                </div>
            </div>

            <!-- 5. GÉNERO -->
            <div class="section">
                <h3>5. Género Principal</h3>
                <div class="chips">
                    {% for g in genres %}
                    <label class="chip">
                        <input type="radio" name="genre" value="{{ g }}" required>
                        <span>{{ g }}</span>
                    </label>
                    {% endfor %}
                </div>
            </div>

            <button type="submit" class="submit-btn">Confirmar Filtros de {{ player[2] }} ➔</button>
        </form>
    </div>

    <script>
        function limitDecades(checkbox) {
            const checked = document.querySelectorAll('input[name="decades"]:checked');
            if (checked.length > 3) {
                checkbox.checked = false;
                alert('Podes escolher no máximo 3 décadas!');
            }
        }
        function validateForm() {
            const checked = document.querySelectorAll('input[name="decades"]:checked');
            const err = document.getElementById('decade-error');
            if (checked.length === 0) {
                err.style.display = 'block';
                return false;
            }
            err.style.display = 'none';
            return true;
        }
    </script>
</body>
</html>
"""

FILTER_SUMMARY_HTML = """
<!DOCTYPE html>
<html lang="pt">
<head>
    <meta charset="UTF-8">
    <title>Resultado dos Filtros - Sessão #{{ session_id }}</title>
    <style>
        body {
            background-color: #121212; color: #fff;
            font-family: 'Segoe UI', sans-serif; padding: 40px 20px; text-align: center;
        }
        .box {
            max-width: 780px; margin: 0 auto; background: #1e1e1e;
            padding: 30px; border-radius: 14px; border: 1px solid #333;
        }
        h1 { color: #f5c518; margin-top: 0; }
        table {
            width: 100%; border-collapse: collapse; margin: 20px 0;
            background: #161616; border-radius: 8px; overflow: hidden;
        }
        th, td { padding: 12px; border-bottom: 1px solid #2e2e2e; text-align: left; }
        th { background: #262626; color: #f5c518; }
        .status-ok {
            background: #1b4332; border: 1px solid #40916c; padding: 18px;
            border-radius: 10px; margin-top: 20px;
        }
        .status-conflict {
            background: #4a151b; border: 1px solid #e63946; padding: 18px;
            border-radius: 10px; margin-top: 20px;
        }
        .btn {
            display: inline-block; background: #f5c518; color: #000;
            font-weight: bold; padding: 14px 26px; border-radius: 8px;
            text-decoration: none; margin-top: 15px;
        }
    </style>
</head>
<body>
    <div class="box">
        <h1>📊 Escolhas de Filtro da Ronda #1</h1>
        <table>
            <thead>
                <tr>
                    <th>Jogador</th>
                    <th>Décadas</th>
                    <th>Duração</th>
                    <th>Idade</th>
                    <th>Rating</th>
                    <th>Género</th>
                </tr>
            </thead>
            <tbody>
                {% for c in choices %}
                <tr>
                    <td><strong>{{ c[0] }}</strong></td>
                    <td>{{ c[1] }}</td>
                    <td>{{ c[2] }}</td>
                    <td>{{ c[3] }}</td>
                    <td>⭐ {{ c[4] }}+</td>
                    <td>{{ c[5] }}</td>
                </tr>
                {% endfor %}
            </tbody>
        </table>

        {% if not had_conflict %}
        <div class="status-ok">
            <h2>✅ Filtros Compatíveis! ({{ matching_count }} filmes encontrados)</h2>
            <p>Existe consenso suficiente para avançar diretamente para a escolha dos filmes.</p>
            <a href="/" class="btn">Pronto para a Etapa 5 (Escolher Filmes) ➔</a>
        </div>
        {% else %}
        <div class="status-conflict">
            <h2>⚔️ Conflito de Filtros Detetado! ({{ matching_count }} filmes na interseção estrita)</h2>
            <p>Filtros em disputa: <strong>{{ conflict_fields }}</strong></p>
            <p>Como a combinação exata tem menos de 3 filmes, vão disputar o filtro no <strong>Torneio de Jogo do Galo</strong>!</p>
            <a href="/" class="btn">Ir para o Torneio de Jogo do Galo (Etapa 4) ⚔️</a>
        </div>
        {% endif %}
    </div>
</body>
</html>
"""


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
    return render_template_string(HOME_HTML, top_movies=top_movies, total_movies=total_movies)


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
            decades = ",".join(request.form.getlist("decades")[:3])
            duration = request.form.get("duration")
            certificate = request.form.get("certificate", "ANY")
            rating = float(request.form.get("rating", 8.0))
            genre = request.form.get("genre")

            cur.execute("""
                INSERT INTO public.filter_choice (
                    id_round, id_player, year_choice, certificate_choice,
                    duration_choice, rating_choice, genres_choice
                ) VALUES (%s, %s, %s, %s, %s, %s, %s);
            """, (round_id, id_player, decades, certificate, duration, rating, genre))
            conn.commit()

        # Verificar que jogadores ainda faltam votar nesta ronda
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
        return render_template_string(
            FILTER_TURN_HTML,
            session_id=session_id,
            player=pending_players[0],
            total_players=total_players,
            decades=decades,
            certificates=certificates,
            genres=genres,
        )

    return redirect(url_for("evaluate_filters", session_id=session_id))


@app.route("/session/<int:session_id>/filters/result")
def evaluate_filters(session_id):
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id_round FROM public.filter_round
            WHERE id_session = %s ORDER BY round_number DESC LIMIT 1;
        """, (session_id,))
        round_id = cur.fetchone()[0]

        cur.execute("""
            SELECT sp.player_name, fc.year_choice, fc.duration_choice,
                   fc.certificate_choice, fc.rating_choice, fc.genres_choice
            FROM public.filter_choice fc
            JOIN public.session_player sp ON sp.id_player = fc.id_player
            WHERE fc.id_round = %s
            ORDER BY sp.player_order ASC;
        """, (round_id,))
        choices = cur.fetchall()

        # Analisar interseção (o que é comum) vs união
        decade_sets = [set(c[1].split(",")) for c in choices if c[1]]
        common_decades = set.intersection(*decade_sets) if decade_sets else set()
        union_decades = set.union(*decade_sets) if decade_sets else set()

        dur_set = {c[2] for c in choices if c[2]}
        cert_set = {c[3] for c in choices if c[3] and c[3] != "ANY"}
        max_min_rating = max(float(c[4]) for c in choices if c[4] is not None)
        genre_set = {c[5] for c in choices if c[5]}

        # Testar interseção estrita (ex: se escolheram géneros diferentes, só filmes que tenham ambos os géneros)
        strict_decades = list(common_decades) if common_decades else list(union_decades)
        strict_durations = list(dur_set) if len(dur_set) == 1 else []
        strict_genres = list(genre_set) if len(genre_set) == 1 else []

        # Verificar quantos filmes cumprem simultaneamente TODAS as exigências de todos os jogadores
        matching_strict = [
            m for m in filter_movies_in_db(
                list(common_decades) if common_decades else ["__NONE__"],
                list(cert_set) if len(cert_set) <= 1 else ["__NONE__"],
                list(dur_set) if len(dur_set) == 1 else ["__NONE__"],
                max_min_rating,
                list(genre_set) if len(genre_set) == 1 else ["__NONE__"],
            )
        ]

        conflicts = []
        if not common_decades:
            conflicts.append("Décadas")
        if len(dur_set) > 1:
            conflicts.append("Duração")
        if len(cert_set) > 1:
            conflicts.append("Restrição de Idade")
        if len(genre_set) > 1:
            conflicts.append("Género")

        had_conflict = len(matching_strict) < 3
        conflict_str = ", ".join(conflicts) if conflicts else "Combinação demasiado restrita (< 3 filmes)"

        new_status = "TIE_FILTER" if had_conflict else "SWIPING"
        cur.execute("""
            UPDATE public.filter_round
            SET had_conflict = %s, conflict_fields = %s
            WHERE id_round = %s;
        """, (had_conflict, conflict_str if had_conflict else None, round_id))

        cur.execute("""
            UPDATE public.game_session SET status = %s WHERE id_session = %s;
        """, (new_status, session_id))

    conn.commit()
    conn.close()

    return render_template_string(
        FILTER_SUMMARY_HTML,
        session_id=session_id,
        choices=choices,
        had_conflict=had_conflict,
        matching_count=len(matching_strict),
        conflict_fields=conflict_str,
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000)