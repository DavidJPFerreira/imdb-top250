import os
from contextlib import contextmanager
from pathlib import Path
import psycopg2
from psycopg2 import pool
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env" if (BASE_DIR / ".env").exists() else BASE_DIR.parent / ".env"
load_dotenv(dotenv_path=ENV_PATH)

ALL_DURATION_CODES = {"<1h30", "1h30-2h", "2h-2h30", ">2h30"}

_db_pool = None


def _get_pool():
    global _db_pool
    if _db_pool is None:
        database_url = os.getenv("DATABASE_URL", "").strip()
        max_conn = int(os.getenv("DB_MAX_CONN", "10"))

        if database_url:
            # Garante que usa SSL caso uses a Connection String direta do Neon
            if "sslmode=" not in database_url and "localhost" not in database_url:
                sep = "&" if "?" in database_url else "?"
                database_url = f"{database_url}{sep}sslmode=require"
            _db_pool = pool.ThreadedConnectionPool(
                minconn=1,
                maxconn=max_conn,
                dsn=database_url
            )
        else:
            host = os.getenv("DB_HOST", "localhost").strip()
            _db_pool = pool.ThreadedConnectionPool(
                minconn=1,
                maxconn=max_conn,
                host=host,
                port=os.getenv("DB_PORT", "5432").strip(),
                dbname=os.getenv("DB_NAME", "neondb").strip(),
                user=os.getenv("DB_USER", "postgres").strip(),
                password=os.getenv("DB_PASSWORD", "").strip(),
                sslmode=os.getenv("DB_SSLMODE", "disable" if host == "localhost" else "require"),
            )
    return _db_pool


class _PooledConnection:
    """Devolve a ligação ao pool automaticamente quando .close() é chamado."""
    def __init__(self, dpool, raw_conn):
        self._pool = dpool
        self._conn = raw_conn
        self._closed = False

    def cursor(self, *args, **kwargs):
        return self._conn.cursor(*args, **kwargs)

    def commit(self):
        return self._conn.commit()

    def rollback(self):
        return self._conn.rollback()

    def close(self):
        if not self._closed and self._conn is not None:
            self._closed = True
            self._pool.putconn(self._conn)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            try:
                self._conn.rollback()
            except Exception:
                pass
        self.close()


def get_connection():
    dpool = _get_pool()
    return _PooledConnection(dpool, dpool.getconn())


@contextmanager
def get_db_connection():
    conn = get_connection()
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# Alias para evitar qualquer ImportError entre get_db_conn e get_db_connection
get_db_conn = get_db_connection


def ensure_schema_updates():
    """
    Atualiza a estrutura das tabelas e cria os índices UNIQUE instantaneamente,
    sem bloquear o arranque do Flask com traduções.
    """
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                ALTER TABLE public.movie
                ADD COLUMN IF NOT EXISTS plot TEXT,
                ADD COLUMN IF NOT EXISTS plot_pt TEXT,
                ADD COLUMN IF NOT EXISTS plot_en TEXT,
                ADD COLUMN IF NOT EXISTS plot_es TEXT;

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

                -- Limpar eventuais duplicados históricos antes de criar restrições UNIQUE
                DELETE FROM public.filter_choice a USING public.filter_choice b
                WHERE a.ctid < b.ctid
                  AND a.id_round = b.id_round AND a.id_player = b.id_player;

                DELETE FROM public.movie_choice a USING public.movie_choice b
                WHERE a.ctid < b.ctid
                  AND a.id_session = b.id_session AND a.id_round = b.id_round
                  AND a.id_player = b.id_player AND a.id_movie = b.id_movie;

                DELETE FROM public.movie_final_choice a USING public.movie_final_choice b
                WHERE a.ctid < b.ctid
                  AND a.id_session = b.id_session AND a.id_player = b.id_player;

                CREATE UNIQUE INDEX IF NOT EXISTS uq_filter_choice_round_player
                ON public.filter_choice (id_round, id_player);

                CREATE UNIQUE INDEX IF NOT EXISTS uq_movie_choice_vote
                ON public.movie_choice (id_session, id_round, id_player, id_movie);

                CREATE UNIQUE INDEX IF NOT EXISTS uq_movie_final_choice_player
                ON public.movie_final_choice (id_session, id_player);
            """)
        conn.commit()


def get_dynamic_filter_options():
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT (year / 10) * 10 AS decade
                FROM public.movie
                WHERE is_in_top250 = TRUE AND year IS NOT NULL
                ORDER BY decade ASC;
            """)
            decades = [f"{int(row[0])}s" for row in cur.fetchall()]

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

    return decades, certificates, genres


def filter_movies_in_db(decades_list, cert_list, duration_list, min_rating, genres_list, lang="pt"):
    """Retorna os filmes do Top 250 com a sinopse já traduzida na língua pedida (índice 9)."""
    plot_col = {
        "pt": "COALESCE(NULLIF(TRIM(plot_pt), ''), NULLIF(TRIM(plot_en), ''), plot)",
        "es": "COALESCE(NULLIF(TRIM(plot_es), ''), NULLIF(TRIM(plot_en), ''), plot)",
    }.get(lang, "COALESCE(NULLIF(TRIM(plot_en), ''), plot)")

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT id_movie, original_title, year, certificate, duration,
                       duration_minutes, rating, genres, poster_url, {plot_col}
                FROM public.movie
                WHERE is_in_top250 = TRUE
                ORDER BY rating DESC, vote_count DESC;
            """)
            all_movies = cur.fetchall()

    matching = []
    for m in all_movies:
        m_id, title, year, cert, dur_str, dur_min, rating, m_genres, poster, plot_txt = m

        # 1. Décadas
        if decades_list and "ANY" not in decades_list and year is not None:
            m_decade = f"{(int(year) // 10) * 10}s"
            if m_decade not in decades_list:
                continue

        # 2. Restrição de Idade (Certificate)
        if cert_list and "ANY" not in cert_list:
            if not cert or cert not in cert_list:
                continue

        # 3. Duração Contínua: <1h30 (<90m), 1h30-2h (90-120m), 2h-2h30 (121-150m), >2h30 (>150m)
        if (
            duration_list
            and "ANY" not in duration_list
            and set(duration_list) != ALL_DURATION_CODES
            and dur_min is not None
        ):
            dur_ok = False
            for d_code in duration_list:
                if d_code == "<1h30" and dur_min < 90:
                    dur_ok = True
                elif d_code in ("1h30-2h", "=2h") and 90 <= dur_min <= 120:
                    dur_ok = True
                elif d_code in ("2h-2h30", "=2h30") and 120 < dur_min <= 150:
                    dur_ok = True
                elif d_code == ">2h30" and dur_min > 150:
                    dur_ok = True
            if not dur_ok:
                continue

        # 4. Rating Mínimo
        if min_rating and rating is not None:
            if float(rating) < float(min_rating):
                continue

        # 5. Géneros
        if genres_list and "ANY" not in genres_list and m_genres:
            movie_g_set = {g.strip() for g in m_genres.split(",")}
            if not movie_g_set.intersection(set(genres_list)):
                continue

        matching.append(m)

    return matching