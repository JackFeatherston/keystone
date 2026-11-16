"""Re-embed every stored row with the current embed() (e.g. after adding the nomic task prefixes).

    cd backend && python -m app.reembed

Content is unchanged, only the vectors are recomputed, so this is safe to re-run.
"""
from .db import supabase as sb
from .embeddings import embed


def main():
    rows = sb.table("embeddings").select("id,content").execute().data
    for i, r in enumerate(rows, 1):
        sb.table("embeddings").update({"embedding": embed(r["content"])}).eq("id", r["id"]).execute()
        if i % 50 == 0:
            print(f"{i}/{len(rows)}")
    print(f"re-embedded {len(rows)} rows")


if __name__ == "__main__":
    main()
