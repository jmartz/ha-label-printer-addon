# Multi-arch Python base (works on both Pi/aarch64 and x86/amd64 HA hardware).
FROM python:3.12-slim

# DejaVu gives us a real bold TrueType font inside the container
# (the script's preferred "arialbd.ttf" doesn't exist on Linux).
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir flask brother_ql Pillow

WORKDIR /app
COPY print_server.py label_render.py /app/

EXPOSE 8099
CMD ["python", "print_server.py"]
