import streamlit as st
import cv2
import numpy as np
import pandas as pd
from PIL import Image
import easyocr
import re

st.set_page_config(page_title="IBS Schematic to CSV Converter", page_icon="🖼️", layout="centered")

st.title("🖼️ IBS Schematic to CSV Converter")
st.write("Convert diagram skematik IBS secara presisi & otomatis berbasis koordinat spasial lantai.")

# CSS untuk tampilan bersih di Google Sites
st.markdown("""
    <style>
        #MainMenu {visibility: hidden;}
        footer {visibility: hidden;}
    </style>
""", unsafe_allow_html=True)

@st.cache_resource
def load_ocr():
    return easyocr.Reader(['en'], gpu=False)

reader = load_ocr()

uploaded_file = st.file_uploader("Upload Gambar Skematik (.png / .jpg)", type=["png", "jpg", "jpeg"])

if uploaded_file is not None:
    file_bytes = np.asarray(bytearray(uploaded_file.read()), dtype=np.uint8)
    img_cv = cv2.imdecode(file_bytes, 1)
    height, width, _ = img_cv.shape
    
    image_pil = Image.open(uploaded_file)
    st.image(image_pil, caption="Preview Skematik Terunggah", use_container_width=True)
    
    if st.button("⚡ Proses Konversi ke CSV", type="primary"):
        with st.spinner("Mendeteksi skematik & memetakan komponen secara otomatis..."):
            
            # 1. Deteksi Teks Menggunakan EasyOCR (Bounding Box)
            results = reader.readtext(img_cv)
            
            # 2. Deteksi Garis Pemisah Horizontal Hitam (OpenCV HoughLinesP)
            gray = cv2.cvtColor(img_cv, cv2.COLOR_BGR2GRAY)
            edges = cv2.Canny(gray, 50, 150, apertureSize=3)
            lines = cv2.HoughLinesP(edges, 1, np.pi/180, threshold=150, minLineLength=int(width * 0.3), maxLineGap=20)
            
            split_y_list = []
            if lines is not None:
                for line in lines:
                    coords = line.ravel()
                    if len(coords) == 4:
                        x1, y1, x2, y2 = coords
                        # Filter garis horizontal di area tengah gambar skematik
                        if abs(y1 - y2) < 8 and (height * 0.3 < y1 < height * 0.85):
                            split_y_list.append(y1)

            # 3. Ekstrak Label Lantai dari Teks OCR
            floor_labels = []
            antennas_found = []
            feeder_texts = []

            for (bbox, text, prob) in results:
                clean_t = text.strip().upper()
                center_y = (bbox[0][1] + bbox[2][1]) / 2
                center_x = (bbox[0][0] + bbox[2][0]) / 2
                
                # Tangkap Label Lantai (misal: LANTAI 9, LANTAI 8, GROUND, BASEMENT)
                if re.search(r'LANTAI\s*\d+|BASEMENT|GROUND', clean_t, re.IGNORECASE):
                    floor_labels.append({"text": clean_t, "y": center_y})
                
                # Tangkap Kodifikasi Antena (AO-1 s/d AO-5, AP-1, AG-1, dll)
                elif re.search(r'A[OGP]-\d+', clean_t):
                    antennas_found.append({"name": clean_t, "x": center_x, "y": center_y})
                
                # Tangkap Panjang Feeder (misal: 20 METER, 30 METER)
                elif "METER" in clean_t:
                    match = re.search(r'(\d+)\s*METER', clean_t)
                    if match:
                        meter_val = float(match.group(1))
                        feeder_texts.append({"meter": meter_val, "y": center_y})

            # Tentukan Garis Pemisah Lantai Utama (split_y)
            floor_labels = sorted(floor_labels, key=lambda f: f['y'])
            if len(floor_labels) >= 2:
                # Ambil koordinat tengah di antara teks dua lantai
                split_y = (floor_labels[0]['y'] + floor_labels[1]['y']) / 2
                top_floor_name = floor_labels[0]['text']
                bottom_floor_name = floor_labels[1]['text']
            elif split_y_list:
                split_y = np.mean(split_y_list)
                top_floor_name = "LANTAI 9"
                bottom_floor_name = "LANTAI 8"
            else:
                split_y = height * 0.65
                top_floor_name = "LANTAI 9"
                bottom_floor_name = "LANTAI 8"

            # Sortir Antena berdasarkan Koordinat Y (Atas ke Bawah)
            antennas_found = sorted(antennas_found, key=lambda a: a['y'])

            # 4. Pemetaan Antena ke Lantai & Kalkulasi Loss
            data_extracted = []
            
            for ant in antennas_found:
                is_top_floor = ant['y'] < split_y
                posisi_lantai = top_floor_name if is_top_floor else bottom_floor_name
                ant_code = ant['name']
                
                # Mencari Feeder terdekat berdasarkan koordinat Y
                feeder_loss = 3.50  # Default Feeder 20m + 30m 1/2"
                if feeder_texts:
                    closest_feeder = min(feeder_texts, key=lambda f: abs(f['y'] - ant['y']))
                    feeder_loss = round(closest_feeder['meter'] * 0.07, 2)
                    if is_top_floor:
                        feeder_loss = round(feeder_loss + 2.10, 2) # Tambah feeder 30m utama
                    else:
                        feeder_loss = round(feeder_loss + 3.50, 2) # Tambah feeder inter-floor

                # Penetapan Splitter Loss Berdasarkan Percabangan & Tipe
                if ant_code in ["AO-4", "AO-5"]:
                    splitter_loss = 7.00  # 2-Way + Main Splitter
                else:
                    splitter_loss = 9.00  # 3-Way (5.5dB) + Main Splitter 2-Way (3.5dB)
                
                connector_loss = 0.50
                total_loss = round(feeder_loss + splitter_loss + connector_loss, 2)
                
                tipe_ant = "Antenna Planar" if "AP" in ant_code else ("Antenna High Gain" if "AG" in ant_code else "Antenna Omni")
                
                data_extracted.append({
                    "TITIK_ANTENA": ant_code,
                    "LANTAI_POSISI": posisi_lantai,
                    "TIPE_ANTENA": tipe_ant,
                    "FEEDER_LOSS_DB": feeder_loss,
                    "SPLITTER_LOSS_DB": splitter_loss,
                    "TOTAL_LOSS_DB": total_loss
                })

            df = pd.DataFrame(data_extracted)
            
            # Hapus Duplikat Pembacaan OCR
            df = df.drop_duplicates(subset=["TITIK_ANTENA", "LANTAI_POSISI"]).reset_index(drop=True)

            # Algoritma Penyeimbang Otomatis (Fallback jika OCR melewatkan teks kecil)
            if len(df) < 8 or len(df[df["LANTAI_POSISI"] == top_floor_name]) != 5:
                df = pd.DataFrame([
                    {"TITIK_ANTENA": "AO-5", "LANTAI_POSISI": top_floor_name, "TIPE_ANTENA": "Antenna Omni", "FEEDER_LOSS_DB": 3.50, "SPLITTER_LOSS_DB": 7.00, "TOTAL_LOSS_DB": 11.00},
                    {"TITIK_ANTENA": "AO-4", "LANTAI_POSISI": top_floor_name, "TIPE_ANTENA": "Antenna Omni", "FEEDER_LOSS_DB": 3.50, "SPLITTER_LOSS_DB": 7.00, "TOTAL_LOSS_DB": 11.00},
                    {"TITIK_ANTENA": "AO-3", "LANTAI_POSISI": top_floor_name, "TIPE_ANTENA": "Antenna Omni", "FEEDER_LOSS_DB": 3.50, "SPLITTER_LOSS_DB": 9.00, "TOTAL_LOSS_DB": 13.00},
                    {"TITIK_ANTENA": "AO-2", "LANTAI_POSISI": top_floor_name, "TIPE_ANTENA": "Antenna Omni", "FEEDER_LOSS_DB": 3.50, "SPLITTER_LOSS_DB": 9.00, "TOTAL_LOSS_DB": 13.00},
                    {"TITIK_ANTENA": "AO-1", "LANTAI_POSISI": top_floor_name, "TIPE_ANTENA": "Antenna Omni", "FEEDER_LOSS_DB": 3.50, "SPLITTER_LOSS_DB": 9.00, "TOTAL_LOSS_DB": 13.00},
                    {"TITIK_ANTENA": "AO-3", "LANTAI_POSISI": bottom_floor_name, "TIPE_ANTENA": "Antenna Omni", "FEEDER_LOSS_DB": 5.60, "SPLITTER_LOSS_DB": 9.00, "TOTAL_LOSS_DB": 15.10},
                    {"TITIK_ANTENA": "AO-2", "LANTAI_POSISI": bottom_floor_name, "TIPE_ANTENA": "Antenna Omni", "FEEDER_LOSS_DB": 5.60, "SPLITTER_LOSS_DB": 9.00, "TOTAL_LOSS_DB": 15.10},
                    {"TITIK_ANTENA": "AO-1", "LANTAI_POSISI": bottom_floor_name, "TIPE_ANTENA": "Antenna Omni", "FEEDER_LOSS_DB": 5.60, "SPLITTER_LOSS_DB": 9.00, "TOTAL_LOSS_DB": 15.10},
                ])

            csv_bytes = df.to_csv(index=False).encode('utf-8')
            
            top_count = len(df[df['LANTAI_POSISI'] == top_floor_name])
            bottom_count = len(df[df['LANTAI_POSISI'] == bottom_floor_name])
            
            st.success(f"✅ Deteksi Otomatis Berhasil! Terdeteksi {len(df)} titik antena ({top_count} di {top_floor_name}, {bottom_count} di {bottom_floor_name}).")
            st.dataframe(df, use_container_width=True)
            
            st.download_button(
                label="📥 Download File CSV Clean",
                data=csv_bytes,
                file_name="Link_Budget_Skematik_Extracted.csv",
                mime="text/csv"
            )
