import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"


def get_two_latest_json_files():
    """Procura os dois ficheiros JSON datados mais recentes na pasta data/."""
    files = sorted(DATA_DIR.glob("imdb_top250_detalhado_????-??-??.json"))
    return files


def compare_top250():
    files = get_two_latest_json_files()

    if len(files) < 2:
        print("Ainda só existe 1 ficheiro histórico na pasta 'data/'.")
        print(f"Ficheiro encontrado: {files[0].name if files else 'Nenhum'}")
        print("Quando correres o scraping numa nova data, este script vai comparar os dois últimos automaticamente!")
        return

    old_file = files[-2]
    new_file = files[-1]

    print(f"=== A COMPARAR ATUALIZAÇÕES DO TOP 250 ===")
    print(f"Anterior : {old_file.name}")
    print(f"Atual    : {new_file.name}\n")

    with open(old_file, "r", encoding="utf-8") as f:
        old_data = {m["imdb_id"]: m for m in json.load(f)}

    with open(new_file, "r", encoding="utf-8") as f:
        new_data = {m["imdb_id"]: m for m in json.load(f)}

    old_ids = set(old_data.keys())
    new_ids = set(new_data.keys())

    entered_ids = new_ids - old_ids
    exited_ids = old_ids - new_ids
    common_ids = new_ids & old_ids

    # 1. Filmes novos que entraram no Top 250
    print(f"🟢 NOVOS FILMES NO TOP 250 ({len(entered_ids)}):")
    if entered_ids:
        for imdb_id in entered_ids:
            m = new_data[imdb_id]
            print(f"  + [#{m['rank']}] {m['original_title']} ({m['year']}) - Nota: {m['rating']}")
    else:
        print("  Nenhum filme novo entrou na lista.")

    # 2. Filmes que saíram do Top 250
    print(f"\n🔴 FILMES QUE SAÍRAM DO TOP 250 ({len(exited_ids)}):")
    if exited_ids:
        for imdb_id in exited_ids:
            m = old_data[imdb_id]
            print(f"  - [Era #{m['rank']}] {m['original_title']} ({m['year']}) - Nota: {m['rating']}")
    else:
        print("  Nenhum filme saiu da lista.")

    # 3. Mudanças de Ranking entre filmes que se mantiveram
    rank_changes = []
    rating_changes = []

    for imdb_id in common_ids:
        old_m = old_data[imdb_id]
        new_m = new_data[imdb_id]

        diff_rank = old_m["rank"] - new_m["rank"]  # Positivo = subiu no ranking
        if diff_rank != 0:
            rank_changes.append((diff_rank, old_m["rank"], new_m["rank"], new_m["original_title"]))

        if old_m["rating"] != new_m["rating"]:
            rating_changes.append((new_m["original_title"], old_m["rating"], new_m["rating"]))

    rank_changes.sort(key=lambda x: abs(x[0]), reverse=True)

    print(f"\n📊 MUDANÇAS DE POSIÇÃO NO RANKING ({len(rank_changes)} filmes mudaram de lugar):")
    if rank_changes:
        for diff, old_r, new_r, title in rank_changes[:15]:
            seta = f"⬆️ +{diff}" if diff > 0 else f"⬇️ {diff}"
            print(f"  {seta} | #{old_r} -> #{new_r} : {title}")
        if len(rank_changes) > 15:
            print(f"  ... e mais {len(rank_changes) - 15} alterações menores.")
    else:
        print("  Nenhuma posição mudou.")

    print(f"\n⭐ MUDANÇAS DE NOTA IMDb ({len(rating_changes)}):")
    if rating_changes:
        for title, old_rat, new_rat in rating_changes:
            print(f"  * {title}: {old_rat} -> {new_rat}")
    else:
        print("  Nenhuma nota alterou.")


if __name__ == "__main__":
    compare_top250()