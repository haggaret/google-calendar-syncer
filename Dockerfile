FROM python:3.6.5-alpine

RUN mkdir /app
RUN mkdir /app/cache

COPY google-calendar-syncer.py /app/
COPY requirements.txt /app/

WORKDIR /app

RUN pip install -r requirements.txt

ENTRYPOINT ["python","/app/google-calendar-syncer.py"]
CMD ["-h"]
