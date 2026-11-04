FROM {{python}}

ENV PYTHONUNBUFFERED=1 \
    PORT=8000{{args}}

WORKDIR /workspace

{{deps}}

COPY . /workspace
{{project}}
ENV PATH="/workspace/.venv/bin:$PATH"

CMD {{cmd}}
