import json
import os
from pathlib import Path
import psycopg2
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
    """Cria as tabelas de filmes, histórico e todas as tabelas do jogo e analytics."""
    with conn.cursor() as cur:
        cur.execute("""
            -- 1. Tabela de Filmes
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
                poster_url TEXT
            );

            -- 2. Histórico mensal de Rankings (para estudo futuro)
            CREATE TABLE IF NOT EXISTS public.movie_rank_history (
                id_history SERIAL PRIMARY KEY,
                id_movie INTEGER NOT NULL REFERENCES public.movie(id_movie) ON DELETE CASCADE,
                recorded_at DATE NOT NULL,
                rank INTEGER NOT NULL,
                rating DECIMAL(3,1),
                vote_count BIGINT,
                UNIQUE(id_movie, recorded_at)
            );

            -- 3. Sessão de Jogo (Partida de 2 a 4 jogadores)
            CREATE TABLE IF NOT EXISTS public.game_session (
                id_session SERIAL PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                finished_at TIMESTAMP,
                num_players SMALLINT NOT NULL,
                status VARCHAR(50) DEFAULT 'SETUP', -- SETUP, FILTERING, TIE_FILTER, SWIPING, TOP3_VOTE, TIE_FINAL, FINISHED
                winning_movie_id INTEGER REFERENCES public.movie(id_movie) ON DELETE SET NULL
            );

            -- 4. Jogadores da Sessão (Apenas nome inserido no ecrã)
            CREATE TABLE IF NOT EXISTS public.session_player (
                id_player SERIAL PRIMARY KEY,
                id_session INTEGER NOT NULL REFERENCES public.game_session(id_session) ON DELETE CASCADE,
                player_order SMALLINT NOT NULL,
                player_name VARCHAR(100) NOT NULL
            );

            -- 5. Ronda de Filtros (Guarda o filtro final aplicado na ronda)
            CREATE TABLE IF NOT EXISTS public.filter_round (
                id_round SERIAL PRIMARY KEY,
                id_session INTEGER NOT NULL REFERENCES public.game_session(id_session) ON DELETE CASCADE,
                round_number SMALLINT NOT NULL,
                had_conflict BOOLEAN DEFAULT FALSE,
                applied_min_year INTEGER,
                applied_max_year INTEGER,
                applied_certificate VARCHAR(50),
                applied_max_duration INTEGER,
                applied_min_rating DECIMAL(3,1),
                applied_genre VARCHAR(100)
            );

            -- 6. Escolhas Individuais de Filtro de cada Jogador (Para a Dashboard de Admin)
            CREATE TABLE IF NOT EXISTS public.filter_choice (
                id_filter_choice SERIAL PRIMARY KEY,
                id_round INTEGER NOT NULL REFERENCES public.filter_round(id_round) ON DELETE CASCADE,
                id_player INTEGER NOT NULL REFERENCES public.session_player(id_player) ON DELETE CASCADE,
                year_choice INTEGER,
                certificate_choice VARCHAR(50),
                duration_choice INTEGER,
                rating_choice DECIMAL(3,1),
                genres_choice VARCHAR(255),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- 7. Votos nos Filmes ("Gosto"=1, "Não Gosto"=0, "Já Vi"=2)
            CREATE TABLE IF NOT EXISTS public.movie_choice (
                id_movie_choice SERIAL PRIMARY KEY,
                id_session INTEGER NOT NULL REFERENCES public.game_session(id_session) ON DELETE CASCADE,
                id_round INTEGER NOT NULL REFERENCES public.filter_round(id_round) ON DELETE CASCADE,
                id_player INTEGER NOT NULL REFERENCES public.session_player(id_player) ON DELETE CASCADE,
                id_movie INTEGER NOT NULL REFERENCES public.movie(id_movie) ON DELETE CASCADE,
                vote_type SMALLINT NOT NULL, -- 0 = Nao gosto, 1 = Gosto, 2 = Ja vi (descartado)
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- 8. Os 3 Filmes Finalistas apurados na Sessão
            CREATE TABLE IF NOT EXISTS public.final_top3_movie (
                id_top3 SERIAL PRIMARY KEY,
                id_session INTEGER NOT NULL REFERENCES public.game_session(id_session) ON DELETE CASCADE,
                id_movie INTEGER NOT NULL REFERENCES public.movie(id_movie) ON DELETE CASCADE
            );

            -- 9. Voto Final de cada Jogador nos 3 Finalistas
            CREATE TABLE IF NOT EXISTS public.movie_final_choice (
                id_movie_final_choice SERIAL PRIMARY KEY,
                id_session INTEGER NOT NULL REFERENCES public.game_session(id_session) ON DELETE CASCADE,
                id_player INTEGER NOT NULL REFERENCES public.session_player(id_player) ON DELETE CASCADE,
                id_movie INTEGER NOT NULL REFERENCES public.movie(id_movie) ON DELETE CASCADE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- 10. Registo de Torneios de Desempate (Jogo do Galo nos Filtros / Pedra-Papel-Tesoura no Final)
            CREATE TABLE IF NOT EXISTS public.tiebreaker_match (
                id_match SERIAL PRIMARY KEY,
                id_session INTEGER NOT NULL REFERENCES public.game_session(id_session) ON DELETE CASCADE,
                id_round INTEGER REFERENCES public.filter_round(id_round) ON DELETE SET NULL,
                game_type VARCHAR(50) NOT NULL, -- 'TIC_TAC_TOE' ou 'ROCK_PAPER_SCISSORS'
                stage VARCHAR(50) NOT NULL,     -- 'FILTER_CONFLICT' ou 'FINAL_MOVIE_CONFLICT'
                player1_id INTEGER NOT NULL REFERENCES public.session_player(id_player) ON DELETE CASCADE,
                player2_id INTEGER NOT NULL REFERENCES public.session_player(id_player) ON DELETE CASCADE,
                winner_player_id INTEGER NOT NULL REFERENCES public.session_player(id_player) ON DELETE CASCADE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
    conn.commit()


def list_to_str(val):
    if isinstance(val, list):
        return ", ".join(str(x) for x in val if x)
    return str(val) if val is not None else None


def sync_json_to_postgres():
    if not JSON_FILE.exists():
        raise FileNotFoundError(
            f"Ficheiro JSON não encontrado em {JSON_FILE}. Corre primeiro o update_imdb_top250_detailed.py!"
        )

    with open(JSON_FILE, "r", encoding="utf-8") as f:
        movies = json.load(f)

    conn = get_connection()
    init_db(conn)

    with conn.cursor() as cur:
        cur.execute("UPDATE public.movie SET is_in_top250 = FALSE, current_rank = NULL;")

        for m in movies:
            imdb_id = m.get("imdb_id")
            if not imdb_id:
                continue

            cur.execute("""
                INSERT INTO public.movie (
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
                    duration_minutes = EXCLUDED.duration_minutes,
                    rating = EXCLUDED.rating,
                    vote_count = EXCLUDED.vote_count,
                    metascore = EXCLUDED.metascore,
                    poster_url = EXCLUDED.poster_url
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

        cur.execute("SELECT COUNT(*) FROM public.movie WHERE is_in_top250 = TRUE;")
        ativos = cur.fetchone()[0]
        cur.execute("""
            SELECT COUNT(*) FROM information_schema.tables 
            WHERE table_schema = 'public';
        """)
        total_tabelas = cur.fetchone()[0]

    conn.close()
    print(f"Etapa 1 Concluída! {ativos} filmes sincronizados via JSON e {total_tabelas} tabelas prontas no PostgreSQL.")


if __name__ == "__main__":
    sync_json_to_postgres()