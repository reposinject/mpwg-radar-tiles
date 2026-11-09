# Exact composite cooker for m7g Phase 1

This branch pins the **exact** custom cooker that was running on the box publisher
(masked-bilinear, 61-color `mpwg-clean-2026-10-61`, reclaim/deletion disabled).

## Hashes (2026-11-09)

- Package content aggregate (py/json + pyproject/requirements): `d7ce9c78f80d65b68979e76c5388a9e63cfdddbfccd61e01b6993e95fc2c1583`
- `cooker.py`: `7ba0677fc4120acc41363691c016fc7f776bb5df26b8ed697e7d5276e0c4eb36`
- Deploy tarball SHA256: `ab9e133d7a7cceb032ded7a78e479934540a2e0fcf70c85c9f558f7035f5b0cd`
- Nov 7 snapshot tarball SHA256: `bd6b573195c9e1cffa208cd02884a93254812c47c4ec3b87bc2a05a34cc54be9` (differs only by reclaim patch in `cooker.py` + `config.py` presence)

Do **not** merge until Phase 1 overnight private test and handoff review are complete.
