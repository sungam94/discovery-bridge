"""Sound analysis service: mood and voice estimates for the tracks of the published playlists.

Runs in its own container (analysis/Dockerfile). It reads the bridge database read-only, fetches each track's
30 s preview from Deezer, runs the Essentia models on it and writes the result to sound.sqlite, which the bridge
reads for the mood footer on the playlist covers. The code must stay importable on Python 3.11 without the bridge.
"""
