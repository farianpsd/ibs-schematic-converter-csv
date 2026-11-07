from flask import Flask, render_template, request, jsonify
import os
import json
from google import genai
from google.genai import types
from PIL import Image

app = Flask(__name__)

# Paste API Key yang sudah dibuat di Google AI Studio di sini
GEMINI_API_KEY = "PASTE_GEMINI_API_KEY_ANDA_DI_SINI"
client = genai.Client(api_key=GEMINI_API_KEY)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/convert', methods=['POST'])
def convert():
    if 'file' not in request.files:
        return jsonify({'error': 'File tidak ditemukan'}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'File belum dipilih'}), 400

    try:
        # Buka stream file gambar menggunakan PIL
        image = Image.open(file.stream)

        # Prompt khusus instruksi analisis IBS Skematik
        prompt = """
        Kamu adalah sistem pakar analisis skematik IBS (In-Building System).
        Analisis gambar skematik yang diberikan dan ekstraksi seluruh data antena distribusi.

        ATURAN EXCLUSION (ABAIKAN):
        - Abaikan Antenna Donor di sebelah kiri.
        - Abaikan Title Block, Legenda REMARKS, Tabel Approval/Revisi di sisi paling kanan.
        
        ALUR PENELUSURAN DARI OUTPUT REPEATER KE ANTENA:
        1. Identifikasi nama lantai tepat untuk setiap antena berdasarkan pembatas garis horizontal horizontal dan label nama lantai (misal LANTAI 1, LANTAI 2, dsb).
        2. Analisis tipe antena: Lingkaran Cyan/Biru = Antenna Omni, Segitiga Cyan/Biru = Antenna Planar.
        3. Berikan kode penamaan berurutan dari antena paling atas ke paling bawah (AO-1, AO-2, dst. atau AP-1, AP-2, dst.).
        4. Telusuri jalur kabel dari output Repeater ke setiap antena:
           - Hitung total panjang Feeder 1/2" (dalam Meter) lalu hitung Feeder Loss (asumsi 1/2" = 0.07 dB/m).
           - Identifikasi jumlah & tipe Splitter pada jalur tersebut (Splitter 2-Way = 3.5 dB, 3-Way = 6.0 dB, 4-Way = 7.0 dB).
           - Hitung TOTAL_LOSS_DB = FEEDER_LOSS_DB + SPLITTER_LOSS_DB + 0.5 (jumper/connector).

        Kembalikan respon HANYA dalam format JSON array murni tanpa markdown/penjelasan tambahan dengan struktur persis seperti berikut:
        [
          {
            "TITIK_ANTENA": "AO-7",
            "LANTAI_POSISI": "LANTAI 2",
            "TIPE_ANTENA": "Antenna Omni",
            "FEEDER_LOSS_DB": 3.5,
            "SPLITTER_LOSS_DB": 7.0,
            "TOTAL_LOSS_DB": 11.0
          }
        ]
        """

        # Panggil Gemini Vision API
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[image, prompt],
            config=types.GenerateContentConfig(
                response_mime_type="application/json"
            )
        )

        # Parse data JSON dari AI
        data = json.loads(response.text)

        return jsonify({
            'success': True,
            'count': len(data),
            'data': data
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
