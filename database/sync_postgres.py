import os
import re
import pandas as pd
import psycopg2
from dotenv import load_dotenv

# Carrega as credenciais do ficheiro .env
load_dotenv()

CSV_FILE = "imdb_top250_detalhado_latest.csv"


def get_connection():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=os.getenv("DB_PORT", "5432"),
        dbname=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
    )


def parse_duration_minutes(duration_str):
    if pd.isna(duration_str):
        return None
    hours = re.search(r"(\d+)h", str(duration_str))
    mins = re.search(r"(\d+)m", str(duration_str))
    total = 0
    if hours:
        total += int(hours.group(1)) * 60
    if mins:
        total += int(mins.group(1))
    return total if total > 0 else None


def extract_imdb_id(url):
    if pd.isna(url):
        return None
    match = re.search(r"(tt\d+)", str(url))
    return match.group(1) if match else None


def init_db(conn):
    with conn.cursor() as cur:
        # Tabela principal de filmes (sintaxe PostgreSQL)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS movie (
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
                genres VARCHAR(255),
                languages VARCHAR(255),
                countries VARCHAR(255),
                link VARCHAR(255),
                poster_url TEXT
            );
        """)

        # Tabela de histórico para estudo futuro
        cur.execute("""
            CREATE TABLE IF NOT EXISTS movie_rank_history (
                id_history SERIAL PRIMARY KEY,
                id_movie INTEGER NOT NULL REFERENCES movie(id_movie) ON DELETE CASCADE,
                recorded_at DATE NOT NULL,
                rank INTEGER NOT NULL,
                rating DECIMAL(3,1),
                vote_count BIGINT,
                UNIQUE(id_movie, recorded_at)
            );
        """)
    conn.commit()


def sync_csv_to_postgres():
    df = pd.read_csv(CSV_FILE)
    conn = get_connection()
    init_db(conn)

    with conn.cursor() as cur:
        # 1. Marca todos os filmes como fora do Top 250 antes de atualizar os 250 atuais
        cur.execute("UPDATE movie SET is_in_top250 = FALSE, current_rank = NULL;")

        for _, row in df.iterrows():
            imdb_id = extract_imdb_id(row["Link"])
            if not imdb_id:
                continue

            duration_min = parse_duration_minutes(row["Duração"])
            data_extracao = row.get(
                "Data_Extracao", pd.Timestamp.today().strftime("%Y-%m-%d")
            )
            metascore = (
                int(row["Metascore"]) if pd.notna(row["Metascore"]) else None
            )

            # 2. UPSERT no PostgreSQL e devolve o id_movie gerado/existente
            cur.execute("""
                INSERT INTO movie (
                    imdb_id, is_in_top250, current_rank, data_upd,
                    original_title, year, certificate, duration, duration_minutes,
                    rating, vote_count, metascore, directors, writers, stars,
                    genres, languages, countries, link, poster_url
                ) VALUES (
                    %s, TRUE, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (imdb_id) DO UPDATE SET
                    is_in_top250 = TRUE,
                    current_rank = EXCLUDED.current_rank,
                    data_upd = EXCLUDED.data_upd,
                    rating = EXCLUDED.rating,
                    vote_count = EXCLUDED.vote_count,
                    metascore = EXCLUDED.metascore,
                    poster_url = EXCLUDED.poster_url
                RETURNING id_movie;
            """, (
                imdb_id, int(row["Rank"]), data_extracao,
                row["Nome Original"], int(row["Ano"]),
                str(row["Classificação Etária"]) if pd.notna(row["Classificação Etária"]) else None,
                str(row["Duração"]) if pd.notna(row["Duração"]) else None,
                duration_min, float(row["Pontuação IMDb"]),
                int(row["Contagem de Votos"]), metascore,
                str(row["Diretor"]) if pd.notna(row["Diretor"]) else None,
                str(row["Writers"]) if pd.notna(row["Writers"]) else None,
                str(row["Stars"]) if pd.notna(row["Stars"]) else None,
                str(row["Género"]) if pd.notna(row["Género"]) else None,
                str(row["Language"]) if pd.notna(row["Language"]) else None,
                str(row["Country of Origin"]) if pd.notna(row["Country of Origin"]) else None,
                row["Link"],
                str(row["Capa"]) if pd.notna(row["Capa"]) else None,
            ))

            id_movie = cur.fetchone()[0]

            # 3. Guarda o registo no histórico mensal
            cur.execute("""
                INSERT INTO movie_rank_history (id_movie, recorded_at, rank, rating, vote_count)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (id_movie, recorded_at) DO UPDATE SET
                    rank = EXCLUDED.rank,
                    rating = EXCLUDED.rating,
                    vote_count = EXCLUDED.vote_count;
            """, (
                id_movie, data_extracao, int(row["Rank"]),
                float(row["Pontuação IMDb"]), int(row["Contagem de Votos"]),
            ))

        conn.commit()

        cur.execute("SELECT COUNT(*) FROM movie WHERE is_in_top250 = TRUE;")
        ativos = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM movie WHERE is_in_top250 = FALSE;")
        historico = cur.fetchone()[0]

    conn.close()
    print(f"PostgreSQL sincronizado! Ativos no Top 250: {ativos} | Guardados fora do Top: {historico}")


if __name__ == "__main__":
    sync_csv_to_postgres()