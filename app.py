import os
import json
import base64
from flask import Flask, render_template, request, jsonify
import google.generativeai as genai
from PIL import Image
import io

app = Flask(__name__)

# Ganti dengan API Key Gemini dari Google AI Studio kamu
GEMINI_API_KEY = "YOUR_GEMINI_API_KEY"
genai.configure(api_key=GEMINI_API_KEY)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/chat', methods=['POST'])
def chat():
    try:
        # Terima gambar dan/atau teks dari obrolan
        file = request.files.get('file')
        user_message = request.form.get('message', '')

        if not file:
            return jsonify({'response': 'Silakan lampirkan gambar skematik IBS yang ingin diekstrak.'})

        # Buka gambar menggunakan PIL
        image_bytes = file.read()
        image = Image.open(io.BytesIO(image_bytes))

        # Prompt khusus Gemini Vision untuk ekstraksi node IBS
        prompt = """
        Kamu adalah pakar analisis skematik IBS (In-Building System).
        Analisis gambar skematik ini dan ekstrak SELURUH titik antena distribusi beserta komponen jalurnya secara presisi.

        ATURAN EXCLUSION:
        - Abaikan Antenna Donor (di area luar/kiri) beserta kabel Feeder 7/8".
        - Abaikan Title Block, Legenda REMARKS, dan Tabel Approval/Revisi di sisi paling kanan.

        INSTRUKSI EKSTRAKSI VISUAL:
        1. DETEKSI SEMUA ANTENA: Cari SEMUA simbol antena (lingkaran atau segitiga warna Cyan/Biru) di UJUNG KANAN kabel Feeder 1/2".
        2. Beri kode TITIK_ANTENA berurutan dari paling bawah ke paling atas pada gambar (AO-1, AO-2, AO-3, dst.).
        3. Identifikasi LANTAI_POSISI dari label teks nama lantai terdekat (misal: LANTAI 1, LANTAI 2, BASEMENT, dsb.).
        4. TIPE_ANTENA: "Antenna Omni" (lingkaran) atau "Antenna Planar" (segitiga).
        5. KABEL_LIST: Masukkan seluruh angka panjang kabel Feeder 1/2" (Meter) dari Repeater ke antena tersebut sebagai array angka.
        6. SPLITTER_LIST: Masukkan seluruh jenis splitter (2-Way, 3-Way, 4-Way, Coupler) dari Repeater ke antena tersebut sebagai array string.

        Kembalikan HANYA JSON murni array of objects tanpa format markdown tambahan:
        [
          {
            "TITIK_ANTENA": "AO-1",
            "LANTAI_POSISI": "LANTAI 1",
            "TIPE_ANTENA": "Antenna Omni",
            "KABEL_LIST": [20, 15, 30],
            "SPLITTER_LIST": ["2-Way", "2-Way", "3-Way"]
          }
        ]
        """

        # Panggil Gemini Vision API (gemini-1.5-pro / gemini-2.0-flash)
        model = genai.GenerativeModel('gemini-1.5-pro')
        response = model.generate_content([prompt, image])
        
        # Bersihkan format teks JSON
        clean_text = response.text.replace('```json', '').replace('```', '').strip()
        parsed_data = json.loads(clean_text)

        if isinstance(parsed_data, dict):
            for key in parsed_data:
                if isinstance(parsed_data[key], list):
                    parsed_data = parsed_data[key]
                    break

        # Logika Kalkulasi Presisi Python
        final_results = []
        for item in parsed_data:
            kabel_list = item.get('KABEL_LIST', [])
            splitters = item.get('SPLITTER_LIST', [])

            # Hitung Feeder Loss (0.07 dB/m)
            total_m = sum([float(c) for c in kabel_list if str(c).replace('.','',1).isdigit()])
            feeder_loss = round(total_m * 0.07, 2)

            # Hitung Splitter Loss
            splitter_loss = 0.0
            for s in splitters:
                s_str = str(s).lower()
                if '2' in s_str: splitter_loss += 3.5
                elif '3' in s_str: splitter_loss += 6.0
                elif '4' in s_str: splitter_loss += 7.0

            splitter_loss = round(splitter_loss, 2)
            total_loss = round(feeder_loss + splitter_loss + 0.5, 2)

            final_results.append({
                "TITIK_ANTENA": item.get("TITIK_ANTENA"),
                "LANTAI_POSISI": item.get("LANTAI_POSISI"),
                "TIPE_ANTENA": item.get("TIPE_ANTENA"),
                "FEEDER_LOSS_DB": feeder_loss,
                "SPLITTER_LOSS_DB": splitter_loss,
                "TOTAL_LOSS_DB": total_loss
            })

        return jsonify({
            'success': True,
            'count': len(final_results),
            'data': final_results
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
