import json
import os
import time
from pathlib import Path
import psycopg2
import requests
from dotenv import load_dotenv

try:
    from deep_translator import MyMemoryTranslator
except ImportError:
    MyMemoryTranslator = None

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


def translate_text_gtx(text, target_lang):
    """
    Usa a API JSON direta do Google (client=gtx), que é muito mais rápida
    e não sofre o bloqueio imediato da página HTML mobile do deep-translator.
    """
    url = "https://translate.googleapis.com/translate_a/single"
    params = {
        "client": "gtx",
        "sl": "en",
        "tl": target_lang,
        "dt": "t",
        "q": text,
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    resp = requests.get(url, params=params, headers=headers, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return "".join(part[0] for part in data[0] if part and part[0])


def translate_batch_chunk(texts, target_lang):
    """
    Junta até 12 sinopses num único pedido HTTP separadas por quebras de linha
    especiais, reduzindo 250 pedidos para apenas ~20 pedidos no total!
    """
    separator = "\n[[[SEP]]]\n"
    joined = separator.join(texts)

    for attempt in range(3):
        try:
            translated_joined = translate_text_gtx(joined, target_lang)
            parts = [p.strip() for p in translated_joined.split("[[[SEP]]]")]
            if len(parts) == len(texts):
                return parts
            # Se o separador foi alterado na tradução, traduz individualmente com pausa
            break
        except Exception as e:
            print(f"   ⚠️ Aviso no lote ({target_lang}), tentativa {attempt + 1}/3: {e}")
            time.sleep(2.0 * (attempt + 1))

    # Fallback individual (GTX -> MyMemoryTranslator)
    results = []
    mm_lang = "pt-PT" if target_lang == "pt" else "es-ES"
    for t in texts:
        val = None
        try:
            val = translate_text_gtx(t, target_lang)
            time.sleep(0.35)
        except Exception:
            if MyMemoryTranslator is not None:
                try:
                    val = MyMemoryTranslator(source="en-GB", target=mm_lang).translate(t[:500])
                except Exception:
                    val = None
        results.append(val)
    return results


def sync_json_to_postgres():
    if not JSON_FILE.exists():
        raise FileNotFoundError(f"Ficheiro JSON não encontrado em {JSON_FILE}.")

    with open(JSON_FILE, "r", encoding="utf-8") as f:
        movies = json.load(f)

    if not isinstance(movies, list) or len(movies) < 200:
        raise ValueError(
            f"Abortado por segurança: O JSON contém apenas {len(movies) if isinstance(movies, list) else 0} filmes."
        )

    conn = get_connection()
    init_db(conn)

    print("1/2 📥 A guardar os 250 filmes na Base de Dados PostgreSQL...")
    with conn.cursor() as cur:
        cur.execute("UPDATE public.movie SET is_in_top250 = FALSE, current_rank = NULL;")

        for m in movies:
            imdb_id = m.get("imdb_id")
            if not imdb_id:
                continue

            plot_en = (m.get("plot") or "").strip()

            cur.execute("""
                INSERT INTO public.movie (
                    imdb_id, is_in_top250, current_rank, data_upd,
                    original_title, year, certificate, duration, duration_minutes,
                    rating, vote_count, metascore, directors, writers, stars,
                    genres, languages, countries, link, poster_url,
                    plot, plot_en
                ) VALUES (
                    %s, TRUE, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (imdb_id) DO UPDATE SET
                    is_in_top250 = TRUE,
                    current_rank = EXCLUDED.current_rank,
                    data_upd = EXCLUDED.data_upd,
                    duration = EXCLUDED.duration,
                    duration_minutes = EXCLUDED.duration_minutes,
                    rating = EXCLUDED.rating,
                    vote_count = EXCLUDED.vote_count,
                    metascore = EXCLUDED.metascore,
                    poster_url = EXCLUDED.poster_url,
                    plot = EXCLUDED.plot,
                    plot_en = EXCLUDED.plot_en
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
                plot_en, plot_en,
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

    # 2. Traduzir em lotes de 12 filmes apenas os que ainda não têm plot_pt ou plot_es
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id_movie, original_title, COALESCE(NULLIF(TRIM(plot_en), ''), NULLIF(TRIM(plot), ''))
            FROM public.movie
            WHERE (
                plot_pt IS NULL OR TRIM(plot_pt) = '' OR plot_pt = plot
                OR plot_es IS NULL OR TRIM(plot_es) = '' OR plot_es = plot
            )
            AND COALESCE(NULLIF(TRIM(plot_en), ''), NULLIF(TRIM(plot), '')) IS NOT NULL
            ORDER BY id_movie ASC;
        """)
        to_translate = cur.fetchall()

    if not to_translate:
        conn.close()
        print("✅ Todos os 250 filmes já têm sinopses traduzidas em PT, EN e ES!")
        return

    print(f"2/2 🌍 A traduzir {len(to_translate)} sinopses em lotes rápidos (PT e ES)...")
    chunk_size = 12
    for i in range(0, len(to_translate), chunk_size):
        chunk = to_translate[i : i + chunk_size]
        ids = [row[0] for row in chunk]
        texts_en = [row[2].strip() for row in chunk]

        pts = translate_batch_chunk(texts_en, "pt")
        time.sleep(0.5)
        ess = translate_batch_chunk(texts_en, "es")
        time.sleep(0.5)

        with conn.cursor() as cur:
            for m_id, pt_val, es_val in zip(ids, pts, ess):
                cur.execute("""
                    UPDATE public.movie
                    SET plot_pt = COALESCE(%s, plot_pt),
                        plot_es = COALESCE(%s, plot_es)
                    WHERE id_movie = %s;
                """, (pt_val, es_val, m_id))
            conn.commit()

        done = min(i + chunk_size, len(to_translate))
        print(f"   ✔ Progresso: {done}/{len(to_translate)} sinopses traduzidas e guardadas...")

    conn.close()
    print("✅ Sucesso! Todas as 250 sinopses estão guardadas em PT, EN e ES no PostgreSQL.")


if __name__ == "__main__":
    sync_json_to_postgres()