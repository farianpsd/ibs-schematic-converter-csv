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
        # Terima banyak gambar sekaligus ('files') atau tunggal ('file') sebagai fallback
        files = request.files.getlist('files')
        if not files or all(f.filename == '' for f in files):
            single_file = request.files.get('file')
            if single_file and single_file.filename != '':
                files = [single_file]
            else:
                return jsonify({'response': 'Silakan lampirkan gambar skematik IBS yang ingin diekstrak.'})

        user_message = request.form.get('message', '')

        # Buka semua gambar menggunakan PIL
        image_list = []
        for f in files:
            if f.filename != '':
                image_bytes = f.read()
                img = Image.open(io.BytesIO(image_bytes))
                image_list.append(img)

        # Prompt khusus Gemini Vision untuk ekstraksi node IBS multi-gambar
        prompt = f"""
        Kamu adalah pakar analisis skematik IBS (In-Building System).
        Terdapat {len(image_list)} gambar skematik yang merupakan SATU KESATUAN SISTEM dari Repeater yang sama (misalnya jalur dari Lantai 4 bercabang ke Lantai 3 dan Lantai 2).
        Analisis SEMUA gambar secara bersamaan, lacak sambungan kabel antar gambar (seperti label 'TO LT ...' dan 'FROM LT ...'), dan ekstrak SELURUH titik antena distribusi beserta komponen jalurnya secara presisi dari output Repeater hingga ke ujung antena.

        ATURAN EXCLUSION:
        - Abaikan Antenna Donor (di area luar/kiri sebelum Repeater) beserta kabel Feeder 7/8" dari Donor ke Repeater.
        - PERHATIAN: Kabel Feeder 7/8" atau 1/2" yang menghubungkan antar lantai SETELAH Repeater/MCM (misalnya 'TO LT 3' / 'FROM LT 4') BUKAN kabel donor — WAJIB dihitung ke dalam jalur antena tujuan!
        - Abaikan Title Block, Legenda REMARKS, dan Tabel Approval/Revisi di sisi paling kanan.

        ATURAN IDENTIFIKASI SPLITTER (SANGAT PENTING):
        - Jangan menebak jenis splitter! Hitung secara teliti jumlah garis kabel/port yang keluar dari sisi kanan simbol splitter merah (mengacu pada legenda REMARKS):
          * 2 garis/port output = "2-Way"
          * 3 garis/port output = "3-Way" (Contoh: splitter utama tepat setelah MCM di Lantai 4 memiliki 3 output: 1 kabel hijau ke Lt 4 dan 2 kabel ungu 'TO LT 3')
          * 4 garis/port output = "4-Way"

        INSTRUKSI EKSTRAKSI VISUAL:
        1. DETEKSI SEMUA ANTENA: Cari SEMUA simbol antena (lingkaran atau segitiga warna Cyan/Biru) di UJUNG KANAN kabel distribusi di seluruh gambar.
        2. Beri kode TITIK_ANTENA sesuai label pada gambar (misal: AO-1, AO-2, AO-3, dst.) atau berurutan dari atas ke bawah per lantai.
        3. Identifikasi LANTAI_POSISI dari label teks nama lantai pada masing-masing bagian gambar (misal: LANTAI 4, LANTAI 3, LANTAI 2, BASEMENT, dsb.).
        4. TIPE_ANTENA: "Antenna Omni" (lingkaran) atau "Antenna Planar" (segitiga).
        5. KABEL_LIST: Masukkan SELURUH angka panjang kabel (Meter) secara berurutan dari output Repeater hingga ke antena tersebut sebagai array angka (termasuk kabel penghubung antar lantai jika berbeda gambar).
        6. SPLITTER_LIST: Masukkan SELURUH jenis splitter/coupler ("2-Way", "3-Way", "4-Way", "Coupler") yang dilewati sinyal dari output Repeater hingga ke antena tersebut sebagai array string.

        Catatan tambahan dari user: {user_message}

        Kembalikan HANYA JSON murni array of objects tanpa format markdown tambahan:
        [
          {{
            "TITIK_ANTENA": "AO-1",
            "LANTAI_POSISI": "LANTAI 4",
            "TIPE_ANTENA": "Antenna Omni",
            "KABEL_LIST": [5, 10, 10, 20, 10],
            "SPLITTER_LIST": ["3-Way", "3-Way", "3-Way"]
          }}
        ]
        """

        # Gabungkan prompt dan semua gambar ke dalam list contents
        contents = [prompt] + image_list

        # Panggil Gemini Vision API
        model = genai.GenerativeModel('gemini-2.5-pro')
        response = model.generate_content(contents)
        
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
