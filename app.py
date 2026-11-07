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

        prompt = """
        Kamu adalah sistem OCR Skematik IBS (In-Building System) tingkat tinggi.
        Tugas utama: Ekstrak SELURUH titik antena (simbol lingkaran/segitiga berwarna Cyan/Biru) TANPA ADA YANG TERLEWAT SATUPUN.

        ATURAN EXCLUSION (ABAIKAN):
        - Abaikan Antenna Donor di sebelah kiri dan Feeder 7/8" 70 Meter.
        - Abaikan Title Block, Legenda REMARKS, Tabel Approval/Revisi di sisi paling kanan.
        
        PETUNJUK DETEKSI ANTENA & JALUR (MANDATORI):
        1. HITUNG SEMUA ANTENA: Pindai seluruh diagram dari atas ke bawah. Setiap lingkaran/segitiga Cyan/Biru adalah 1 titik antena. Urutkan penamaan titik dari paling bawah ke paling atas (AO-1, AO-2, AO-3, dst.).
        2. LANTAI_POSISI: Ambil nama lantai dari garis pembatas horizontal (misal: LANTAI 1, LANTAI 2).
        3. TIPE_ANTENA: Lingkaran Cyan/Biru = "Antenna Omni", Segitiga Cyan/Biru = "Antenna Planar".
        
        4. KABEL_FEEDER_METERS:
           - Telusuri garis dari Repeater hingga ke ujung antena tersebut.
           - Catat SELURUH angka panjang kabel Feeder 1/2" (misal: 20, 15, 25) yang dilewati jalur tersebut dari Repeater ke antena dalam bentuk array angka.
           
        5. SPLITTER_TYPES:
           - Catat SEMUA jenis pembagi sinyal yang dilewati dari Repeater ke antena tersebut.
           - Perhatikan bentuk fisik dan teks label: Splitter 2-Way (pembagi 2 cabang), Splitter 3-Way (pembagi 3 cabang), Splitter 4-Way (pembagi 4 cabang).
           - Masukkan ke array string (misal: ["2-Way", "2-Way", "3-Way"]).

        Kembalikan HANYA format JSON murni array of objects:
        [
          {
            "TITIK_ANTENA": "AO-1",
            "LANTAI_POSISI": "LANTAI 1",
            "TIPE_ANTENA": "Antenna Omni",
            "KABEL_FEEDER_METERS": [20, 15, 30],
            "SPLITTER_TYPES": ["2-Way", "2-Way", "3-Way"]
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
                "temperature": 0.0,
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

        # LOGIKA KALKULASI PYTHON (Dinamis & Presisi)
        final_results = []
        for item in parsed_data:
            kabel_list = item.get('KABEL_FEEDER_METERS', [])
            splitters = item.get('SPLITTER_TYPES', [])

            # 1. Feeder Loss
            total_panjang = 0.0
            if isinstance(kabel_list, list):
                for val in kabel_list:
                    try:
                        total_panjang += float(val)
                    except (ValueError, TypeError):
                        pass

            feeder_loss = round(total_panjang * 0.07, 2)

            # 2. Splitter Loss
            splitter_loss = 0.0
            if isinstance(splitters, list):
                for s in splitters:
                    s_str = str(s).lower()
                    if '2' in s_str:
                        splitter_loss += 3.5
                    elif '3' in s_str:
                        splitter_loss += 6.0
                    elif '4' in s_str:
                        splitter_loss += 7.0

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
