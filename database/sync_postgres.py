import json
import os
from pathlib import Path
import psycopg2
from deep_translator import GoogleTranslator
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = BASE_DIR / ".env" if (BASE_DIR / ".env").exists() else Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=ENV_PATH)

JSON_FILE = BASE_DIR / "data" / "imdb_top250_detalhado_latest.json"


def get_connection():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost").strip(),
        port=os.getenv("DB_PORT", "5432").strip(),
        dbname=os.getenv("DB_NAME", "movie_game").strip(),
        user=os.getenv("DB_USER", "postgres").strip(),
        password=os.getenv("DB_PASSWORD", "").strip(),
    )


def init_db(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS public.movie (
                id_movie SERIAL PRIMARY KEY,
                imdb_id VARCHAR(20) UNIQUE NOT NULL,
                is_in_top250 BOOLEAN DEFAULT TRUE,
                current_rank INTEGER,
                data_upd DATE,
                original_title VARCHAR(255),
                year INTEGER,
                certificate VARCHAR(50),
                duration VARCHAR(50),
                duration_minutes INTEGER,
                rating DECIMAL(3,1),
                vote_count BIGINT,
                metascore INTEGER,
                directors TEXT,
                writers TEXT,
                stars TEXT,
                genres TEXT,
                languages TEXT,
                countries TEXT,
                link VARCHAR(255),
                poster_url TEXT,
                plot TEXT,
                plot_pt TEXT,
                plot_en TEXT,
                plot_es TEXT
            );

            ALTER TABLE public.movie
            ADD COLUMN IF NOT EXISTS plot TEXT,
            ADD COLUMN IF NOT EXISTS plot_pt TEXT,
            ADD COLUMN IF NOT EXISTS plot_en TEXT,
            ADD COLUMN IF NOT EXISTS plot_es TEXT;

            CREATE TABLE IF NOT EXISTS public.movie_rank_history (
                id_history SERIAL PRIMARY KEY,
                id_movie INTEGER NOT NULL REFERENCES public.movie(id_movie) ON DELETE CASCADE,
                recorded_at DATE NOT NULL,
                rank INTEGER NOT NULL,
                rating DECIMAL(3,1),
                vote_count BIGINT,
                UNIQUE(id_movie, recorded_at)
            );
        """)
    conn.commit()


def list_to_str(val):
    if isinstance(val, list):
        return ", ".join(str(x) for x in val if x)
    return str(val) if val is not None else None


def sync_json_to_postgres():
    if not JSON_FILE.exists():
        raise FileNotFoundError(f"Ficheiro JSON não encontrado em {JSON_FILE}.")

    with open(JSON_FILE, "r", encoding="utf-8") as f:
        movies = json.load(f)

    conn = get_connection()
    init_db(conn)

    translator_pt = GoogleTranslator(source="en", target="pt")
    translator_es = GoogleTranslator(source="en", target="es")

    with conn.cursor() as cur:
        cur.execute("UPDATE public.movie SET is_in_top250 = FALSE, current_rank = NULL;")

        for idx, m in enumerate(movies, start=1):
            imdb_id = m.get("imdb_id")
            if not imdb_id:
                continue

            plot_en = m.get("plot") or ""

            # Verifica se este filme já tem sinopse traduzida na BD para não traduzir duas vezes
            cur.execute("SELECT plot_pt, plot_es FROM public.movie WHERE imdb_id = %s;", (imdb_id,))
            existing = cur.fetchone()

            plot_pt = existing[0] if existing and existing[0] else None
            plot_es = existing[1] if existing and existing[1] else None

            if plot_en and (not plot_pt or not plot_es):
                try:
                    plot_pt = translator_pt.translate(plot_en)
                    plot_es = translator_es.translate(plot_en)
                    print(f"[{idx}/250] Sinopse traduzida (PT/ES): {m.get('original_title')}")
                except Exception:
                    plot_pt = plot_pt or plot_en
                    plot_es = plot_es or plot_en

            cur.execute("""
                INSERT INTO public.movie (
                    imdb_id, is_in_top250, current_rank, data_upd,
                    original_title, year, certificate, duration, duration_minutes,
                    rating, vote_count, metascore, directors, writers, stars,
                    genres, languages, countries, link, poster_url,
                    plot, plot_pt, plot_en, plot_es
                ) VALUES (
                    %s, TRUE, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (imdb_id) DO UPDATE SET
                    is_in_top250 = TRUE,
                    current_rank = EXCLUDED.current_rank,
                    data_upd = EXCLUDED.data_upd,
                    duration_minutes = EXCLUDED.duration_minutes,
                    rating = EXCLUDED.rating,
                    vote_count = EXCLUDED.vote_count,
                    metascore = EXCLUDED.metascore,
                    poster_url = EXCLUDED.poster_url,
                    plot = EXCLUDED.plot,
                    plot_en = EXCLUDED.plot_en,
                    plot_pt = COALESCE(public.movie.plot_pt, EXCLUDED.plot_pt),
                    plot_es = COALESCE(public.movie.plot_es, EXCLUDED.plot_es)
                RETURNING id_movie;
            """, (
                imdb_id, m.get("rank"), m.get("data_extracao"),
                m.get("original_title"), m.get("year"), m.get("certificate"),
                m.get("duration"), m.get("duration_minutes"), m.get("rating"),
                m.get("vote_count"), m.get("metascore"),
                list_to_str(m.get("directors")),
                list_to_str(m.get("writers")),
                list_to_str(m.get("stars")),
                list_to_str(m.get("genres")),
                list_to_str(m.get("languages")),
                list_to_str(m.get("countries")),
                m.get("link"), m.get("poster_url"),
                plot_en, plot_pt, plot_en, plot_es,
            ))

            id_movie = cur.fetchone()[0]

            cur.execute("""
                INSERT INTO public.movie_rank_history (id_movie, recorded_at, rank, rating, vote_count)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (id_movie, recorded_at) DO UPDATE SET
                    rank = EXCLUDED.rank,
                    rating = EXCLUDED.rating,
                    vote_count = EXCLUDED.vote_count;
            """, (
                id_movie, m.get("data_extracao"), m.get("rank"),
                m.get("rating"), m.get("vote_count"),
            ))

        conn.commit()
    conn.close()
    print("Sucesso! Filmes e sinopses nas 3 línguas (PT, EN, ES) sincronizados no PostgreSQL.")


if __name__ == "__main__":
    sync_json_to_postgres()