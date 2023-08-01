FROM python:slim-bullseye

COPY requirements.txt /

RUN pip install --upgrade pip \
    && pip install -r /requirements.txt

COPY app /app
CMD /app/mifit_to_influxdb.py
