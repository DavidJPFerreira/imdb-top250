import os
from pathlib import Path
import psycopg2
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env" if (BASE_DIR / ".env").exists() else BASE_DIR.parent / ".env"
load_dotenv(dotenv_path=ENV_PATH)


def get_connection():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost").strip(),
        port=os.getenv("DB_PORT", "5432").strip(),
        dbname=os.getenv("DB_NAME", "movie_game").strip(),
        user=os.getenv("DB_USER", "postgres").strip(),
        password=os.getenv("DB_PASSWORD", "").strip(),
    )


def ensure_schema_updates():
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("""
            ALTER TABLE public.filter_choice
            ALTER COLUMN year_choice TYPE VARCHAR(100) USING year_choice::VARCHAR,
            ALTER COLUMN duration_choice TYPE VARCHAR(100) USING duration_choice::VARCHAR,
            ADD COLUMN IF NOT EXISTS combat_category VARCHAR(50);

            ALTER TABLE public.filter_round
            ADD COLUMN IF NOT EXISTS applied_decades VARCHAR(255),
            ADD COLUMN IF NOT EXISTS applied_duration_code VARCHAR(100),
            ADD COLUMN IF NOT EXISTS conflict_fields VARCHAR(255),
            ADD COLUMN IF NOT EXISTS winning_player_id INTEGER REFERENCES public.session_player(id_player) ON DELETE SET NULL;

            ALTER TABLE public.filter_round
            ALTER COLUMN applied_certificate TYPE VARCHAR(150),
            ALTER COLUMN applied_genre TYPE VARCHAR(255);
        """)
    conn.commit()
    conn.close()


def get_dynamic_filter_options():
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
                if g.strip():
                    genre_set.add(g.strip())
        genres = sorted(genre_set)

    conn.close()
    return decades, certificates, genres


def filter_movies_in_db(decades_list, cert_list, duration_list, min_rating, genres_list):
    """Retorna os filmes completos do Top 250 que cumprem os filtros aplicados."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id_movie, original_title, year, certificate, duration,
                   duration_minutes, rating, genres, poster_url
            FROM public.movie
            WHERE is_in_top250 = TRUE
            ORDER BY rating DESC, vote_count DESC;
        """)
        all_movies = cur.fetchall()
    conn.close()

    matching = []
    for m in all_movies:
        m_id, title, year, cert, dur_str, dur_min, rating, m_genres, poster = m

        # 1. Décadas ('ANY' ou vazio = todas)
        if decades_list and "ANY" not in decades_list and year:
            m_decade = f"{(int(year) // 10) * 10}s"
            if m_decade not in decades_list:
                continue

        # 2. Classificação Etária ('ANY' ou vazio = todas)
        if cert_list and "ANY" not in cert_list:
            if not cert or cert not in cert_list:
                continue

        # 3. Duração ('ANY' ou vazio = todas)
        if duration_list and "ANY" not in duration_list and dur_min is not None:
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

        # 5. Géneros ('ANY' ou vazio = todos)
        if genres_list and "ANY" not in genres_list and m_genres:
            movie_g_set = {g.strip() for g in m_genres.split(",")}
            if not movie_g_set.intersection(set(genres_list)):
                continue

        matching.append(m)

    return matching