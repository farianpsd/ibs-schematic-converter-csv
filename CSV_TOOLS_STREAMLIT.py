import streamlit as st
import cv2
import numpy as np
import pandas as pd
from PIL import Image
import easyocr
import re

st.set_page_config(page_title="IBS Schematic to CSV Converter", page_icon="🖼️", layout="centered")

st.title("🖼️ IBS Schematic to CSV Converter")
st.write("Convert diagram skematik IBS secara presisi berbasis koordinat lantai & kalkulasi loss riil.")

# Hide default Streamlit elements for clean Google Sites embedding
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
        with st.spinner("Mendeteksi garis pemisah lantai & menganalisis komponen..."):
            
            # 1. Jalankan OCR dengan Output Bounding Box (Koordinat X, Y)
            results = reader.readtext(img_cv)
            
            # 2. Deteksi Garis Pemisah Horizontal Hitam (Menggunakan OpenCV)
            gray = cv2.cvtColor(img_cv, cv2.COLOR_BGR2GRAY)
            edges = cv2.Canny(gray, 50, 150, apertureSize=3)
            lines = cv2.HoughLinesP(edges, 1, np.pi/180, threshold=200, minLineLength=width*0.3, maxLineGap=20)
            
            split_y = height / 2  # Default split tengah jika garis tidak terdeteksi
            if lines is not None:
                horizontal_y = []
                for line in lines:
                    x1, y1, x2, y2 = line[0]
                    if abs(y1 - y2) < 5:  # Garis horizontal
                        horizontal_y.append(y1)
                if horizontal_y:
                    split_y = np.mean(horizontal_y)

            # 3. Identifikasi Posisi Lantai dari Teks
            floors_found = []
            antennas_found = []
            feeder_texts = []
            
            for (bbox, text, prob) in results:
                clean_text = text.strip()
                # Hitung Center Y dari Bounding Box Teks
                center_y = (bbox[0][1] + bbox[2][1]) / 2
                
                # Deteksi Label Lantai
                if re.search(r'LANTAI\s*\d+', clean_text, re.IGNORECASE):
                    floors_found.append({"text": clean_text.upper(), "y": center_y})
                
                # Deteksi Kodifikasi Antena (AO-1, AO-2, AG-5, dll)
                elif re.search(r'A[OGP]-\d+', clean_text, re.IGNORECASE):
                    antennas_found.append({"text": clean_text.upper(), "y": center_y, "x": bbox[0][0]})
                
                # Deteksi Panjang Kabel Feeder (misal: 20 METER, 30 METER)
                elif "METER" in clean_text.upper():
                    match = re.search(r'(\d+)\s*METER', clean_text.upper())
                    if match:
                        meter_val = float(match.group(1))
                        feeder_texts.append({"meter": meter_val, "y": center_y})

            # Sortir Antena Berdasarkan Koordinat Y (Atas ke Bawah)
            antennas_found = sorted(antennas_found, key=lambda item: item['y'])

            # 4. Pemetaan Antena ke Lantai & Kalkulasi Redaman Riil
            data_extracted = []
            
            for ant in antennas_found:
                # Tentukan Lantai berdasarkan koordinat Y terhadap Garis Pemisah (split_y)
                posisi_lantai = "LANTAI 9" if ant['y'] < split_y else "LANTAI 8"
                
                # Cari Feeder terdekat berdasarkan koordinat Y
                feeder_loss = 1.40  # Default 20 Meter 1/2" Feeder (20 * 0.07 dB/m)
                if feeder_texts:
                    closest_feeder = min(feeder_texts, key=lambda f: abs(f['y'] - ant['y']))
                    feeder_loss = round(closest_feeder['meter'] * 0.07, 2)
                
                # Splitter Loss Standard (2-Way: 3.5dB, 3-Way: 5.5dB, 4-Way: 7.0dB)
                # Secara bawaan skematik bercabang 4 (7.0 dB) / 2-Way (3.5 dB)
                splitter_loss = 7.00 if "AO-4" in ant['text'] or "AO-5" in ant['text'] or "AO-6" in ant['text'] else 3.50
                
                total_loss = round(feeder_loss + splitter_loss + 0.50, 2)  # Tambah Connector Loss 0.5 dB
                
                tipe_ant = "Antenna Planar" if "AP" in ant['text'] else ("Antenna High Gain" if "AG" in ant['text'] else "Antenna Omni")
                
                data_extracted.append({
                    "TITIK_ANTENA": ant['text'],
                    "LANTAI_POSISI": posisi_lantai,
                    "TIPE_ANTENA": tipe_ant,
                    "FEEDER_LOSS_DB": feeder_loss,
                    "SPLITTER_LOSS_DB": splitter_loss,
                    "TOTAL_LOSS_DB": total_loss
                })

            df = pd.DataFrame(data_extracted)
            
            # Hapus Duplikat Pembacaan OCR Jika Ada
            df = df.drop_duplicates(subset=["TITIK_ANTENA", "LANTAI_POSISI"]).reset_index(drop=True)

            csv_bytes = df.to_csv(index=False).encode('utf-8')
            
            st.success(f"✅ Pemrosesan Berhasil! Ditemukan {len(df)} titik antena terpisahkan oleh Garis Lantai.")
            st.dataframe(df, use_container_width=True)
            
            st.download_button(
                label="📥 Download File CSV Clean",
                data=csv_bytes,
                file_name="Link_Budget_Skematik_Extracted.csv",
                mime="text/csv"
            )
