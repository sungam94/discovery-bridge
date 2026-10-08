# Sound analysis

The optional `sound-analysis` container listens to a 30-second preview of every track in your playlists and
rates its mood and whether it has vocals. The bridge turns the results into the mood line at the bottom of the
playlist covers. This page explains what it does, what it needs, what to expect, and when it is worth turning on.

## What it does

1. **Picks the next track.** It reads `data/bridge.sqlite` (read-only) and takes the tracks of the published
   Spotify playlists, then those of the TIDAL playlists in MA's Discover rows. The playlist closest to complete
   goes first, so the covers get their mood line one after the other instead of all at the end.
2. **Gets a preview.** It looks the track up by ISRC in Deezer's public API (no account, no key) and downloads
   Deezer's 30-second preview clip. At most four requests a second.
3. **Runs the models** on the clip (CPU only, see below) and deletes the clip at once. No audio is kept.
4. **Stores the result** in `data/sound.sqlite`: mood scores, an instrumental score, an embedding, and the facts
   Deezer returned for the track (such as BPM where Deezer has one, rank and release date).
5. **Waits.** When nothing is pending it checks again every 10 minutes. Tracks without a Deezer preview are
   stored as `no_preview` and not asked again; errors are retried after 7 days.

## The models

All three come from the Essentia project of the Music Technology Group (Universitat Pompeu Fabra) and are
downloaded and checksum-verified when the image is built:

| Model | Output | Used for |
|---|---|---|
| Discogs-EffNet | a 1280-value embedding per clip | input for the two heads below; stored for later use |
| MTG-Jamendo mood/theme head | a score for each of 56 moods and themes (dark, energetic, melancholic, epic, relaxing, …) | the mood words on the covers |
| Voice/instrumental head | 0 (vocals) to 1 (instrumental) | the instrumental share of a playlist |

**Licence:** the models are CC BY-NC-SA 4.0, which means non-commercial use only. A private home setup is fine;
do not use them in anything you sell or run as a paid service.

## How the results show up

- A playlist gets a mood line only when at least 90% of its tracks have a result (of any kind) and at least 5
  were actually analysed. Until then its cover has no mood line, so the text does not jump around while the
  queue is being worked through.
- The mood scores of the analysed tracks are averaged. The top mood is shown, plus the second one if it scores
  at least 60% of the first.
- Themes that describe a use rather than a mood (film, advertising, corporate, summer, christmas, …) are skipped.
- The bridge redraws a cover only when its mood line or genres actually change.

The embedding is stored but not used by the covers yet. It is a compact "sounds like" fingerprint per track,
meant for later features such as a map of your music or similarity-based playlists.

## Requirements

- **An x86_64 server whose CPU supports AVX**, which covers practically every Intel or AMD CPU of the last ten
  years. The prebuilt `essentia-tensorflow` package exists only for x86_64 Linux. **It does not run on a
  Raspberry Pi or other ARM boards**; leave the analysis off there. The rest of the bridge works without it.
- **No GPU.** Everything runs on the CPU.
- **Resources:** the compose file limits the container to 2 CPU cores and 2 GB of RAM. In practice it uses
  about 220 MB of RAM. The image is about 0.9 GB, and `sound.sqlite` grows by roughly 12 KB per track (most of
  it the embedding).
- **Internet access to `api.deezer.com`** and Deezer's preview servers.

## What to expect

Measured on an Intel N100 (4 cores, a typical mini PC) over about 4,300 tracks:

- **Speed:** about 5 seconds per track (90% within 7.3 seconds), including the download. That is roughly
  700 tracks an hour. A first fill of a few thousand tracks takes an afternoon; after that only new tracks are
  analysed, a few hundred a week.
- **Coverage:** about 98% of tracks had a Deezer preview, on a library heavy in psytrance, metal and post-rock.
  The lookup is by ISRC, so how popular a track is does not matter; whether Deezer carries it does.
- **BPM:** Deezer reports a BPM for only about a third of the tracks. The analysis does not compute tempo or key
  itself.

## Limits

- **30 seconds is a sample.** Deezer picks the excerpt, usually from the middle of the track. A song that starts
  quiet and ends loud is judged by that one part.
- **SoundCloud tracks are not analysed.** They have no ISRC, so Deezer cannot find them; long DJ sets have no
  useful 30-second preview anyway.
- **The mood model learned from Jamendo music** (Creative Commons releases tagged by their uploaders). Its
  words are hints, not verdicts, and genres far from that catalogue get coarser words.
- **Genres do not come from this model.** The genre labels on the covers come from MusicBrainz tags (and
  SoundCloud tags) through the bridge, not from audio.

## Recommendations

- **Turn it on if you have an x86_64 server.** It costs little (two cores at most, a few hundred MB of RAM),
  needs no account, and gives every cover a mood line. Set `COMPOSE_PROFILES` to include `analysis`, or answer
  yes in `scripts/setup.sh`.
- **Leave it off on a Raspberry Pi** or any ARM board. The covers then simply have no mood line.
- **Let the first fill run.** Covers get their mood line playlist by playlist as it goes. Check progress with
  `docker logs sound-analysis`; every 25 tracks it logs how many are pending.
- **Watch `health.json`.** The `sound` problem appears when the container has written nothing for 6 hours while
  tracks are waiting, which usually means Deezer blocked or changed its API, or the container stopped.
- **Keep `sound.sqlite` in your backups.** The daily backup already includes it. Rebuilding it from scratch
  means redoing all the downloads.
- **Mind Deezer.** The analysis uses Deezer's public API the way a web page would, at a polite rate, and keeps
  no audio. Check Deezer's API terms if you plan anything beyond personal use.

## Troubleshooting

- **The image does not build** with an error from the smoke test: the CPU or architecture is not supported
  (ARM, or a very old CPU without AVX). Remove `analysis` from `COMPOSE_PROFILES`.
- **`Deezer unavailable … next try in N s`** in the logs: Deezer is busy, rate-limiting or unreachable. The
  service backs off (up to 30 minutes) and continues by itself.
- **A cover never gets a mood line:** fewer than 90% of its tracks have a result yet, or fewer than 5 had a
  preview. Very short or very new playlists may stay without one.
