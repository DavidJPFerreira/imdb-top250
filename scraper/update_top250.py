from datetime import datetime
import json
from pathlib import Path
import requests

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

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
        titleText { text }
        originalTitleText { text }
        releaseYear { year }
        certificate { rating }
        runtime { seconds }
        ratingsSummary { aggregateRating voteCount }
        metacritic { metascore { score } }
        primaryImage { url }
        plot { plotText { plainText } }
        titleGenres { genres { genre { text } } }
        principalCredits {
          category { text id }
          credits { name { nameText { text } } }
        }
        spokenLanguages { spokenLanguages { text } }
        countriesOfOrigin { countries { text } }
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


def parse_movie_node(rank, node, today):
    imdb_id = node.get("id")
    original_title = (node.get("originalTitleText") or {}).get("text") or (
        node.get("titleText") or {}
    ).get("text")
    year = (node.get("releaseYear") or {}).get("year")
    certificate = (node.get("certificate") or {}).get("rating") or "Not Rated"

    seconds = (node.get("runtime") or {}).get("seconds")
    duration = format_runtime(seconds)
    duration_minutes = (seconds // 60) if seconds else None

    ratings = node.get("ratingsSummary") or {}
    rating = ratings.get("aggregateRating")
    vote_count = ratings.get("voteCount")
    metascore = ((node.get("metacritic") or {}).get("metascore") or {}).get("score")
    poster_url = (node.get("primaryImage") or {}).get("url")
    plot = ((node.get("plot") or {}).get("plotText") or {}).get("plainText")

    genres_list = (node.get("titleGenres") or {}).get("genres") or []
    genres = [
        g["genre"]["text"]
        for g in genres_list
        if g.get("genre") and g["genre"].get("text")
    ]

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

    lang_list = (node.get("spokenLanguages") or {}).get("spokenLanguages") or []
    languages = [l.get("text") for l in lang_list if l.get("text")]

    country_list = (node.get("countriesOfOrigin") or {}).get("countries") or []
    countries = [c.get("text") for c in country_list if c.get("text")]

    return {
        "rank": rank,
        "imdb_id": imdb_id,
        "original_title": original_title,
        "year": year,
        "certificate": certificate,
        "duration": duration,
        "duration_minutes": duration_minutes,
        "rating": rating,
        "vote_count": vote_count,
        "metascore": metascore,
        "directors": list(dict.fromkeys(directors)),
        "writers": list(dict.fromkeys(writers)),
        "stars": list(dict.fromkeys(stars)),
        "genres": genres,
        "languages": languages,
        "countries": countries,
        "link": f"https://www.imdb.com/title/{imdb_id}/" if imdb_id else None,
        "poster_url": poster_url,
        "plot": plot,
        "data_extracao": today,
    }


def fetch_all_top_250():
    movies = []
    after_cursor = None
    batch_size = 50
    today = datetime.now().strftime("%Y-%m-%d")

    while len(movies) < 250:
        payload = {
            "query": CHART_QUERY,
            "variables": {"first": batch_size, "after": after_cursor},
        }
        response = requests.post(GRAPHQL_URL, json=payload, headers=HEADERS, timeout=20)
        response.raise_for_status()

        data = response.json()
        chart_data = (data.get("data") or {}).get("chartTitles") or {}
        edges = chart_data.get("edges") or []
        if not edges:
            break

        for edge in edges:
            movies.append(
                parse_movie_node(edge.get("currentRank"), edge.get("node") or {}, today)
            )

        print(f"Progresso: {len(movies)}/250 filmes recolhidos (com sinopse)...")
        page_info = chart_data.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        after_cursor = page_info.get("endCursor")

    return movies, today


def main():
    print("A recolher o Top 250 + Sinopses via IMDb GraphQL API...")
    dataset, today = fetch_all_top_250()

    file_dated = DATA_DIR / f"imdb_top250_detalhado_{today}.json"
    file_latest = DATA_DIR / "imdb_top250_detalhado_latest.json"

    with open(file_dated, "w", encoding="utf-8") as f:
        json.dump(dataset, f, ensure_ascii=False, indent=2)

    with open(file_latest, "w", encoding="utf-8") as f:
        json.dump(dataset, f, ensure_ascii=False, indent=2)

    print(f"\nSucesso! {len(dataset)} filmes guardados em JSON com sinopse:\n- {file_latest}\n- {file_dated}")


if __name__ == "__main__":
    main()