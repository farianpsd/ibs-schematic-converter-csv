from flask import Flask, render_template, request, jsonify
import cv2
import pandas as pd
import pytesseract
import re
import os
import io
import numpy as np

app = Flask(__name__)

# Konfigurasi jalur Tesseract OCR di environment Linux PythonAnywhere
if os.path.exists('/usr/bin/tesseract'):
    pytesseract.pytesseract.tesseract_cmd = '/usr/bin/tesseract'

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
        in_memory_file = io.BytesIO()
        file.save(in_memory_file)
        data_bytes = np.frombuffer(in_memory_file.getvalue(), dtype=np.uint8)
        img = cv2.imdecode(data_bytes, cv2.IMREAD_COLOR)

        if img is None:
            return jsonify({'error': 'Gagal membuka format gambar'}), 400

        height, width, _ = img.shape
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

        # -------------------------------------------------------------
        # STEP 1: DETEKSI GARIS PEMBATAS LANTAI & TEKS NAMA LANTAI
        # -------------------------------------------------------------
        # Deteksi Garis Horizontal Hitam Pembatas Lantai
        edges = cv2.Canny(gray, 50, 150, apertureSize=3)
        lines = cv2.HoughLinesP(edges, 1, np.pi/180, threshold=200, minLineLength=width*0.4, maxLineGap=10)
        
        floor_lines_y = []
        if lines is not None:
            for line in lines:
                x1, y1, x2, y2 = line[0]
                if abs(y1 - y2) < 5:  # Garis mendatar presisi
                    floor_lines_y.append(y1)
        
        floor_lines_y = sorted(list(set([round(y, -1) for y in floor_lines_y])))

        # Fast OCR khusus area kiri (X < 35%) untuk membaca nama lantai saja
        crop_left = img[0:height, 0:int(width * 0.35)]
        ocr_left = pytesseract.image_to_data(
            cv2.cvtColor(crop_left, cv2.COLOR_BGR2GRAY), 
            config=r'--oem 3 --psm 6', 
            output_type=pytesseract.Output.DATAFRAME
        )
        ocr_left = ocr_left[ocr_left.text.notnull()]

        detected_floors = []
        for idx, row in ocr_left.iterrows():
            text = str(row['text']).strip().upper()
            if re.search(r'LANTAI\s*\d+|BASEMENT|GROUND|L\d+', text):
                detected_floors.append({"text": text, "y": row['top']})
        
        detected_floors = sorted(detected_floors, key=lambda f: f['y'])

        # Fungsi pemetaan posisi Y ke nama lantai
        def get_floor_name(y_pos):
            if not detected_floors:
                return "LANTAI 1"
            if len(detected_floors) == 1:
                return detected_floors[0]['text']
            
            for i in range(len(detected_floors) - 1):
                mid_y = (detected_floors[i]['y'] + detected_floors[i+1]['y']) / 2
                if y_pos < mid_y:
                    return detected_floors[i]['text']
            return detected_floors[-1]['text']

        # -------------------------------------------------------------
        # STEP 2: DETEKSI ANTENA DI AREA DISTRIBUSI OUTPUT REPEATER
        # -------------------------------------------------------------
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        lower_cyan = np.array([80, 100, 100])
        upper_cyan = np.array([105, 255, 255])
        mask = cv2.inRange(hsv, lower_cyan, upper_cyan)

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        antennas = []
        for cnt in contours:
            area = cv2.contourArea(cnt)
            perimeter = cv2.arcLength(cnt, True)
            
            if 40 < area < 2500 and perimeter > 0:
                M = cv2.moments(cnt)
                if M["m00"] != 0:
                    cX = int(M["m10"] / M["m00"])
                    cY = int(M["m01"] / M["m00"])

                    # BATAS KOORDINAT (EXCLUSION ZONE):
                    # - Mengabaikan Antenna Donor di area kiri (X < 25%)
                    # - Mengabaikan Title Block & Legenda di area kuning kanan (X > 82%)
                    if width * 0.25 < cX < width * 0.82:
                        circularity = (4 * np.pi * area) / (perimeter ** 2)
                        # Membedakan Antenna Omni (Lingkaran) dan Antenna Planar (Segitiga)
                        tipe = "Antenna Omni" if circularity > 0.60 else "Antenna Planar"
                        antennas.append({"x": cX, "y": cY, "tipe": tipe})

        # Urutkan posisi antena dari paling atas ke paling bawah
        antennas = sorted(antennas, key=lambda a: a['y'])

        # -------------------------------------------------------------
        # STEP 3: URUTKAN JALUR & ESTIMASI LOSS UNTUK SETIAP ANTENA
        # -------------------------------------------------------------
        extracted_rows = []
        total_antennas = len(antennas)
        
        for idx, ant in enumerate(antennas, 1):
            posisi_lantai = get_floor_name(ant['y'])
            prefix = "AO" if ant['tipe'] == "Antenna Omni" else "AP"
            ant_name = f"{prefix}-{total_antennas - idx + 1}"

            # Kalkulasi nilai loss berdasarkan posisi lantai dan hierarki cabang
            is_top_floor = "2" in posisi_lantai or "3" in posisi_lantai
            feeder_loss = 4.20 if is_top_floor else 3.50
            splitter_loss = 7.00 if is_top_floor else 10.50
            jumper_loss = 0.50
            total_loss = round(feeder_loss + splitter_loss + jumper_loss, 2)

            extracted_rows.append({
                "TITIK_ANTENA": ant_name,
                "LANTAI_POSISI": posisi_lantai,
                "TIPE_ANTENA": ant['tipe'],
                "FEEDER_LOSS_DB": feeder_loss,
                "SPLITTER_LOSS_DB": splitter_loss,
                "TOTAL_LOSS_DB": total_loss
            })

        df = pd.DataFrame(extracted_rows)

        return jsonify({
            'success': True,
            'count': len(df),
            'data': df.to_dict(orient='records')
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
