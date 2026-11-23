# Provenance

Copied unchanged from [`pyroplast/ai-disclosure-scan`](https://github.com/pyroplast/ai-disclosure-scan) `examples/` (`sites/`, `transcripts/`, `media/`) at commit `9e72975ffb9d52eaca59e54690672ba0995335e2` (0.4.0; identical in 0.5.0). MIT licence, same author.

Everything here is fictional: the Northwind sites, the people in the conversations and the images (gradients drawn by the scanner's `scripts/make_examples.py`, whose metadata imitates what real tools write). No real client data.

Tests serve these files from `127.0.0.1` and scan them in static mode. The pages reference a real chat vendor's CDN script so the scanner recognises the widget; static mode never fetches scripts, so no test touches the public internet. See `docs/decisions.md`, D-012 and D-016.
