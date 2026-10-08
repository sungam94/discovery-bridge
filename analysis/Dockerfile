# Sound analysis service: moods and voice/instrumental per track from Deezer's 30 s previews (src/analysis).
# Build from the repository root: docker compose build sound-analysis
#
# The models are the Essentia TensorFlow models by the Music Technology Group (Universitat Pompeu Fabra),
# https://essentia.upf.edu/models/, licensed CC BY-NC-SA 4.0: non-commercial use only, which this private
# setup is. They are downloaded at build time and not kept in the repository.
FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src MODELS_DIR=/models
WORKDIR /app
COPY analysis/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt
ADD https://essentia.upf.edu/models/music-style-classification/discogs-effnet/discogs-effnet-bs64-1.pb /models/
ADD https://essentia.upf.edu/models/classification-heads/mtg_jamendo_moodtheme/mtg_jamendo_moodtheme-discogs-effnet-1.pb /models/
ADD https://essentia.upf.edu/models/classification-heads/mtg_jamendo_moodtheme/mtg_jamendo_moodtheme-discogs-effnet-1.json /models/
ADD https://essentia.upf.edu/models/classification-heads/voice_instrumental/voice_instrumental-discogs-effnet-1.pb /models/
COPY analysis/models.sha256 /models/
RUN cd /models && sha256sum -c models.sha256 && chmod -R a+r /models
COPY src/analysis ./src/analysis
# loads the models and analyses a generated clip: a wrong wheel, model or output node fails here
RUN python -m analysis.smoke /models
CMD ["python", "-m", "analysis.main"]
