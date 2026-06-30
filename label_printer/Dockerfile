# Multi-arch Python base (works on both Pi/aarch64 and x86/amd64 HA hardware).
FROM python:3.12-slim

# Bundle several real TrueType families so the designer can offer varied
# "lettering": DejaVu (Sans/Serif/Mono + bold/italic), Liberation (Arial- and
# Times-like), and FreeFont (broad Unicode/symbol coverage for special chars).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        fonts-dejavu fonts-liberation fonts-freefont-ttf \
    && rm -rf /var/lib/apt/lists/*

# flask + brother_ql + Pillow for printing; qrcode + python-barcode for the
# designer's QR and barcode elements.
RUN pip install --no-cache-dir flask brother_ql Pillow qrcode python-barcode

WORKDIR /app
COPY print_server.py label_render.py custom_render.py /app/
COPY web /app/web

EXPOSE 8099
CMD ["python", "print_server.py"]
