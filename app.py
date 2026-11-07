from flask import Flask, render_template, request, jsonify
import os
import json
import base64
import requests

app = Flask(__name__)

# API Key Groq Anda
GROQ_API_KEY = "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"

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
        image_bytes = file.read()
        base64_image = base64.b64encode(image_bytes).decode('utf-8')
        mime_type = file.mimetype or 'image/png'

        # Prompt Generik & Dinamis dengan instruksi Spatial Tracing Ketat
        prompt = """
        Kamu adalah sistem pakar OCR & Spatial Analysis skematik IBS (In-Building System).
        Analisis gambar skematik yang diberikan dan ekstrak data mentah untuk setiap titik antena.

        ATURAN EXCLUSION (ABAIKAN):
        - Abaikan Antenna Donor di sebelah kiri dan Feeder 7/8" 70 Meter menuju Antenna Donor.
        - Abaikan Title Block, Legenda REMARKS, Tabel Approval/Revisi di sisi paling kanan.
        
        INSTRUKSI EKSTRAKSI DATA MENTAH (GENERIK & DINAMIS):
        1. TITIK_ANTENA: Berikan penamaan berurutan dari antena paling bawah ke paling atas pada diagram (misal AO-1, AO-2, dst.).
        2. LANTAI_POSISI: Baca nama lantai dari label area teks terdekat (misal: LANTAI 1, LANTAI 2, BASEMENT, ROOFTOP, dsb.).
        3. TIPE_ANTENA: Lingkaran Cyan/Biru = "Antenna Omni", Segitiga Cyan/Biru = "Antenna Planar".
        
        4. PANJANG_TRUNK_METER: 
           - Cari kabel Feeder 1/2" utama yang langsung menghubungkan output Repeater / Splitter Utama menuju ke Splitter di lantai tempat antena tersebut berada.
           - Ambil HANYA 1 nilai panjang kabel trunk untuk jalur lantai tersebut. JANGAN menjumlahkan kabel trunk antar lantai yang berbeda.

        5. PANJANG_CABANG_METER: 
           - Catat angka panjang kabel Feeder 1/2" cabang akhir yang berada di antara Splitter Lantai dan titik antena tersebut (misal: 20, 25, 30, 35).

        6. SPLITTER_TYPES (KRUSIAL - PENELUSURAN DARI AWAL):
           - TELUSURI GARIS JALUR SINYAL DARI TITIK KELUAR (OUTPUT) REPEATER HINGGA KETITIK ANTENA.
           - Catat SEMUA komponen pembagi sinyal (Splitter 2-Way, Splitter 3-Way, Splitter 4-Way, Coupler) yang dilewati garis tersebut dari awal hingga akhir.
           - Contoh: Jika dari Repeater melewati Splitter 2-Way lalu ke Splitter 4-Way lantai, kembalikan ["2-Way", "4-Way"]. Jika hanya melewati 1 Splitter 4-Way, kembalikan ["4-Way"].

        Kembalikan HANYA format JSON murni array of objects tanpa teks/markdown tambahan:
        [
          {
            "TITIK_ANTENA": "AO-1",
            "LANTAI_POSISI": "LANTAI 1",
            "TIPE_ANTENA": "Antenna Omni",
            "PANJANG_TRUNK_METER": 25,
            "PANJANG_CABANG_METER": 25,
            "SPLITTER_TYPES": ["2-Way", "4-Way"]
          }
        ]
        """

        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json"
        }

        VISION_MODELS = [
            "qwen/qwen3.8-27b"
        ]

        res_json = None
        last_error = ""

        for vision_model in VISION_MODELS:
            payload = {
                "model": vision_model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:{mime_type};base64,{base64_image}"
                                }
                            }
                        ]
                    }
                ],
                "temperature": 0.1,
                "response_format": {"type": "json_object"}
            }

            response = requests.post(url, json=payload, headers=headers, timeout=120)
            res_json = response.json()

            if response.status_code == 200:
                break
            else:
                last_error = res_json.get('error', {}).get('message', 'API Error')

        if not res_json or 'choices' not in res_json:
            return jsonify({'error': f"Gagal memproses gambar: {last_error}"}), 400

        raw_text = res_json['choices'][0]['message']['content']
        parsed_data = json.loads(raw_text)
        
        if isinstance(parsed_data, dict):
            for key in parsed_data:
                if isinstance(parsed_data[key], list):
                    parsed_data = parsed_data[key]
                    break

        # LOGIKA KALKULASI MATEMATIS MURNI (TANPA ASSUMPTION / HARDCODE)
        final_results = []
        for item in parsed_data:
            trunk = float(item.get('PANJANG_TRUNK_METER', 0))
            cabang = float(item.get('PANJANG_CABANG_METER', 0))
            splitters = item.get('SPLITTER_TYPES', [])

            # 1. Hitung Feeder Loss = (Trunk + Cabang) * 0.07 dB/m
            total_panjang = trunk + cabang
            feeder_loss = round(total_panjang * 0.07, 2)

            # 2. Hitung Splitter Loss dinamis sesuai kompeonen yang benar-benar terdeteksi
            splitter_loss = 0.0
            for s in splitters:
                s_str = str(s).lower()
                if '2' in s_str:
                    splitter_loss += 3.5
                elif '3' in s_str:
                    splitter_loss += 6.0
                elif '4' in s_str:
                    splitter_loss += 7.0

            splitter_loss = round(splitter_loss, 2)

            # 3. Total Loss = Feeder Loss + Splitter Loss + 0.5 (connector margin)
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
