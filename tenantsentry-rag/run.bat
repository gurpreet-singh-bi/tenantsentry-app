@echo off
echo Installing dependencies...
pip install streamlit pdfplumber PyMuPDF pyyaml loguru --quiet

echo.
echo Starting TenantSentry.ai prototype...
echo Open your browser at: http://localhost:8501
echo.
python -m streamlit run app.py --server.port 8501
