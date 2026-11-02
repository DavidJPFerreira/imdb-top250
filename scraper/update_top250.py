from datetime import datetime
import pandas as pd
import requests

GRAPHQL_URL = "https://api.graphql.imdb.com/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/129.0.0.0 Safari/537.36"
    ),
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Origin": "https://www.imdb.com",
    "Referer": "https://www.imdb.com/",
}

# Query GraphQL que pede exatamente todos os 15 campos de uma só vez
CHART_QUERY = """
query ($first: Int!, $after: String) {
  chartTitles(first: $first, after: $after, chart: {chartType: TOP_RATED_MOVIES}) {
    pageInfo {
      hasNextPage
      endCursor
    }
    edges {
      currentRank
      node {
        id
        titleText {
          text
        }
        originalTitleText {
          text
        }
        releaseYear {
          year
        }
        certificate {
          rating
        }
        runtime {
          seconds
        }
        ratingsSummary {
          aggregateRating
          voteCount
        }
        metacritic {
          metascore {
            score
          }
        }
        primaryImage {
          url
        }
        titleGenres {
          genres {
            genre {
              text
            }
          }
        }
        principalCredits {
          category {
            text
            id
          }
          credits {
            name {
              nameText {
                text
              }
            }
          }
        }
        spokenLanguages {
          spokenLanguages {
            text
          }
        }
        countriesOfOrigin {
          countries {
            text
          }
        }
      }
    }
  }
}
"""


def format_runtime(seconds):
    if not seconds:
        return None
    hours = seconds // 3600
    mins = (seconds % 3600) // 60
    return f"{hours}h {mins}m"


def parse_movie_node(rank, node):
    imdb_id = node.get("id")

    # Nome original (com fallback para o título principal)
    original_title = (node.get("originalTitleText") or {}).get("text") or (
        node.get("titleText") or {}
    ).get("text")

    # Ano
    year = (node.get("releaseYear") or {}).get("year")

    # Classificação Etária (Maiores de que idade)
    certificate = (node.get("certificate") or {}).get("rating") or "Not Rated"

    # Duração
    seconds = (node.get("runtime") or {}).get("seconds")
    duration = format_runtime(seconds)

    # Pontuação e Contagem de votos
    ratings = node.get("ratingsSummary") or {}
    rating = ratings.get("aggregateRating")
    vote_count = ratings.get("voteCount")

    # Metascore
    metascore = ((node.get("metacritic") or {}).get("metascore") or {}).get(
        "score"
    )

    # Capa do filme
    poster_url = (node.get("primaryImage") or {}).get("url")

    # Géneros
    genres_list = (node.get("titleGenres") or {}).get("genres") or []
    genres = [
        g["genre"]["text"]
        for g in genres_list
        if g.get("genre") and g["genre"].get("text")
    ]

    # Diretor, Writers e Stars
    directors, writers, stars = [], [], []
    for group in node.get("principalCredits") or []:
        cat_id = ((group.get("category") or {}).get("id") or "").lower()
        cat_text = ((group.get("category") or {}).get("text") or "").lower()

        names = [
            c["name"]["nameText"]["text"]
            for c in (group.get("credits") or [])
            if c.get("name")
            and c["name"].get("nameText")
            and c["name"]["nameText"].get("text")
        ]

        if "director" in cat_id or "director" in cat_text:
            directors.extend(names)
        elif "writer" in cat_id or "writer" in cat_text:
            writers.extend(names)
        elif "cast" in cat_id or "star" in cat_text or "actor" in cat_text:
            stars.extend(names)

    # Idiomas (Language)
    lang_list = (node.get("spokenLanguages") or {}).get("spokenLanguages") or []
    languages = [l.get("text") for l in lang_list if l.get("text")]

    # País de Origem (Country of origin)
    country_list = (node.get("countriesOfOrigin") or {}).get("countries") or []
    countries = [c.get("text") for c in country_list if c.get("text")]

    return {
        "Rank": rank,
        "Nome Original": original_title,
        "Ano": year,
        "Classificação Etária": certificate,
        "Duração": duration,
        "Pontuação IMDb": rating,
        "Contagem de Votos": vote_count,
        "Metascore": metascore,
        "Diretor": ", ".join(dict.fromkeys(directors)),
        "Writers": ", ".join(dict.fromkeys(writers)),
        "Stars": ", ".join(dict.fromkeys(stars)),
        "Género": ", ".join(genres),
        "Language": ", ".join(languages),
        "Country of Origin": ", ".join(countries),
        "Link": f"https://www.imdb.com/title/{imdb_id}/" if imdb_id else None,
        "Capa": poster_url,
    }


def fetch_all_top_250():
    movies = []
    after_cursor = None
    batch_size = 50  # Pede 50 filmes de cada vez (5 pedidos = 250 filmes)

    while len(movies) < 250:
        payload = {
            "query": CHART_QUERY,
            "variables": {"first": batch_size, "after": after_cursor},
        }

        response = requests.post(
            GRAPHQL_URL, json=payload, headers=HEADERS, timeout=20
        )

        if response.status_code != 200:
            raise RuntimeError(
                f"Erro HTTP {response.status_code}: {response.text[:300]}"
            )

        data = response.json()
        if "errors" in data:
            raise RuntimeError(f"Erro GraphQL: {data['errors']}")

        chart_data = (data.get("data") or {}).get("chartTitles") or {}
        edges = chart_data.get("edges") or []

        if not edges:
            break

        for edge in edges:
            rank = edge.get("currentRank")
            node = edge.get("node") or {}
            movies.append(parse_movie_node(rank, node))

        print(f"Progresso: {len(movies)}/250 filmes recolhidos...")

        page_info = chart_data.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        after_cursor = page_info.get("endCursor")

    return movies


def main():
    print("A recolher o Top 250 completo via IMDb GraphQL API...")
    dataset = fetch_all_top_250()

    if not dataset:
        print("Atenção: Nenhum filme foi retornado.")
        return

    df = pd.DataFrame(dataset)
    today = datetime.now().strftime("%Y-%m-%d")
    df["Data_Extracao"] = today

    filename_dated = f"imdb_top250_detalhado_{today}.csv"
    filename_latest = "imdb_top250_detalhado_latest.csv"

    df.to_csv(filename_dated, index=False, encoding="utf-8-sig")
    df.to_csv(filename_latest, index=False, encoding="utf-8-sig")

    print(
        f"\nSucesso! {len(df)} filmes guardados em:\n- {filename_latest}\n- {filename_dated}"
    )


if __name__ == "__main__":
    main()