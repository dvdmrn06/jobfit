#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
"""

import os
import re
import json
import time
import tempfile
from typing import Optional, Tuple, List, Dict

import requests
import streamlit as st
from dotenv import load_dotenv
from openai import OpenAI

import pdfplumber

try:
    import gdown  # type: ignore
    _HAS_GDOWN = True
except Exception:
    _HAS_GDOWN = False

#Configuración base *******************

st.set_page_config(
    page_title="🔎 JobFit Assistant",
    page_icon="🧭",
)

try:
    # Intentar cargar secrets de Streamlit Cloud
    OPENAI_API_KEY = st.secrets["OPENAI_API_KEY"]
    ASSISTANT_ID = st.secrets.get("ASSISTANT_ID", "asst_TOZMVaL9GI3Y5P2zIeL61aeG")
    JOBS_PDF_FILE_ID = st.secrets.get("JOBS_PDF_FILE_ID", "")
    JOBS_PDF_URL = st.secrets.get("JOBS_PDF_URL", "")
except Exception:
    load_dotenv()
    OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
    ASSISTANT_ID = os.environ.get("ASSISTANT_ID", "asst_TOZMVaL9GI3Y5P2zIeL61aeG")

if not OPENAI_API_KEY:
    st.error("Falta OPENAI_API_KEY en variables de entorno.")
    st.stop()

client = OpenAI(api_key=OPENAI_API_KEY)


#Utilidades de descarga /scrap *****************

def _drive_id_from_url(url: str) -> Optional[str]:
    """Extrae file_id desde URL de Google Drive, si aplica."""
    if not url:
        return None
    # Casos comunes: .../file/d/<ID>/..., o ?id=<ID>
    m = re.search(r"/d/([a-zA-Z0-9_-]+)", url)
    if m:
        return m.group(1)
    m = re.search(r"[?&]id=([a-zA-Z0-9_-]+)", url)
    if m:
        return m.group(1)
    return None


def _resolve_pdf_source(
    arg_file_id: Optional[str],
    arg_pdf_url: Optional[str],
    arg_pdf_path: Optional[str],
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Devuelve (file_id, pdf_url, pdf_path) resolviendo:
    - argumentos de la tool,
    - valores en UI (sidebar),
    - variables de entorno.
    """
    #Tool args
    file_id = arg_file_id or None
    pdf_url = arg_pdf_url or None
    pdf_path = arg_pdf_path or None

    #UI sidebar (si no lo pasó el assistant)
    ui_file_id = st.session_state.get("jobs_pdf_file_id") or ""
    ui_pdf_url = st.session_state.get("jobs_pdf_url") or ""

    if not file_id and ui_file_id.strip():
        file_id = ui_file_id.strip()
    if not pdf_url and ui_pdf_url.strip():
        pdf_url = ui_pdf_url.strip()

    #ENV
    if not file_id:
        env_file_id = os.environ.get("JOBS_PDF_FILE_ID", "").strip()
        if env_file_id:
            file_id = env_file_id
    if not pdf_url:
        env_pdf_url = os.environ.get("JOBS_PDF_URL", "").strip()
        if env_pdf_url:
            pdf_url = env_pdf_url

    #Si pdf_url es de Drive, preferimos quedarnos con file_id
    if pdf_url and not file_id:
        maybe_id = _drive_id_from_url(pdf_url)
        if maybe_id:
            file_id = maybe_id

    return file_id or None, pdf_url or None, pdf_path or None


def _download_pdf_to_temp(file_id: Optional[str], pdf_url: Optional[str], pdf_path: Optional[str]) -> str:
    """
    Descarga o localiza el PDF y devuelve la ruta local.
    - Si pdf_path existe, lo usa.
    - Si file_id, descarga desde Drive.
    - Si pdf_url, descarga por HTTP.
    """
    #Ruta local ya existente
    if pdf_path and os.path.exists(pdf_path):
        return pdf_path

    tmp_dir = tempfile.mkdtemp(prefix="scrape_jobs_")
    local_path = os.path.join(tmp_dir, "ofertas.pdf")

    if file_id:
        drive_direct = f"https://drive.google.com/uc?id={file_id}"
        if _HAS_GDOWN:
            gdown.download(drive_direct, local_path, quiet=True)
        else:
            # Fallback sin gdown
            r = requests.get(drive_direct, timeout=30)
            r.raise_for_status()
            with open(local_path, "wb") as f:
                f.write(r.content)
        return local_path

    if pdf_url:
        r = requests.get(pdf_url, timeout=30)
        r.raise_for_status()
        with open(local_path, "wb") as f:
            f.write(r.content)
        return local_path

    raise ValueError(
        "No se proporcionó ni file_id, ni pdf_url, ni pdf_path, y no hay defaults en UI/ENV."
    )



#Extracción y parseo *********************************

def extract_text_from_pdf_local(path: str) -> str:
    text = ""
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                text += page_text + "\n"
    return text


def parse_offers(text: str) -> List[Dict]:
    """
    Parser basado en regex según estructura común en ofertas de infojobs.
    """
    offers: List[Dict] = []
    #Detectar cada oferta usando regex por "Oferta X."
    raw_offers = re.split(r'Oferta\s\d+\.\s', text)
    for raw in raw_offers[1:]:
        offer: Dict[str, object] = {}
        lines = [line.strip() for line in raw.split('\n') if line.strip()]
        content = "\n".join(lines)

        #Título
        m_title = re.match(r'^(.*?)\n', content)
        offer['titulo'] = m_title.group(1) if m_title else ""

        #Empresa
        m_company = re.search(r'^(.*?) - Ofertas de trabajo', content, re.MULTILINE)
        offer['empresa'] = m_company.group(1).strip() if m_company else ""

        #Calificación de la empresa (ej: "4,2 (123)")
        m_rating = re.search(r'(\d+,\d+ \(\d+\))', content)
        offer['calificacion_empresa'] = m_rating.group(1) if m_rating else ""

        #Ubicación (ej: "Madrid (Híbrido)" o "Barcelona (Presencial)")
        m_location = re.search(r'(Madrid|Barcelona|[A-Za-záéíóúñü ]+)\s\([A-Za-záéíóúñü ]+\)', content)
        offer['ubicacion'] = m_location.group(0) if m_location else ""

        #Modalidad
        m_modalidad = re.search(r'(Solo teletrabajo|Híbrido|Presencial)', content)
        offer['modalidad'] = m_modalidad.group(0) if m_modalidad else ""

        #Salario
        m_salary = re.search(r'Salario[: ]+(.*)', content)
        offer['salario'] = m_salary.group(1).strip() if m_salary else ""

        #Experiencia mínima
        m_exp = re.search(r'Experiencia mínima[: ]+(.*)', content)
        offer['experiencia_minima'] = m_exp.group(1).strip() if m_exp else ""

        #Contrato
        m_contract = re.search(r'Contrato[: ]+(.*)', content)
        offer['contrato'] = m_contract.group(1).strip() if m_contract else ""

        #Proceso
        m_process = re.search(r'Proceso[: ]+(.*)', content)
        offer['proceso'] = m_process.group(1).strip() if m_process else ""

        #Fecha de publicación
        m_pub = re.search(r'Publicada[: ]+(.*)', content)
        offer['publicada'] = m_pub.group(1).strip() if m_pub else ""

        #Requisitos (bloque entre "Requisitos" y "Descripción")
        m_reqs = re.search(r'Requisitos\s*(.*?)Descripción', content, re.DOTALL)
        offer['requisitos'] = m_reqs.group(1).strip() if m_reqs else ""

        #Descripción (entre "Descripción" y "Tipo de industria de la oferta")
        m_desc = re.search(r'Descripción\s*(.*?)Tipo de industria de la oferta', content, re.DOTALL)
        offer['descripcion'] = m_desc.group(1).strip() if m_desc else ""

        #Tipo de industria
        m_industry = re.search(r'Tipo de industria de la oferta\s*(.*)', content)
        offer['tipo_industria'] = m_industry.group(1).strip() if m_industry else ""

        #Categoría
        m_category = re.search(r'Categoría\s*(.*)', content)
        offer['categoria'] = m_category.group(1).strip() if m_category else ""

        #Nivel
        m_level = re.search(r'Nivel\s*(.*)', content)
        offer['nivel'] = m_level.group(1).strip() if m_level else ""

        #Personas a cargo
        m_people = re.search(r'Personas a cargo\s*(.*)', content)
        offer['personas_a_cargo'] = m_people.group(1).strip() if m_people else ""

        #Vacantes
        m_vacancies = re.search(r'Vacantes\s*(.*)', content)
        offer['vacantes'] = m_vacancies.group(1).strip() if m_vacancies else ""

        #Horario
        m_schedule = re.search(r'Horario\s*(.*)', content)
        offer['horario'] = m_schedule.group(1).strip() if m_schedule else ""

        #Beneficios sociales
        m_benefits = re.search(r'Beneficios sociales\s*(.*)', content, re.DOTALL)
        if m_benefits:
            benefits_lines = [b.strip() for b in m_benefits.group(1).split('\n') if b.strip()]
            offer['beneficios_sociales'] = benefits_lines
        else:
            offer['beneficios_sociales'] = []

        offers.append(offer)
    return offers


def scrape_jobs_tool(arg_json: Dict) -> str:
    """
    Implementación de la tool 'scrape_jobs'.

    Args esperados (opcionales):
      - file_id: str (Google Drive file id)
      - pdf_url: str (URL directa o compartida)
      - pdf_path: str (ruta local ya existente)

    Devuelve un string JSON con la lista de ofertas.
    Adicionalmente guarda el resultado en 'ofertas.json' en el cwd.
    """
    #Resolver origen del PDF combinando tool args, UI y ENV
    file_id, pdf_url, pdf_path = _resolve_pdf_source(
        arg_json.get("file_id"),
        arg_json.get("pdf_url"),
        arg_json.get("pdf_path"),
    )

    #Descargar / ubicar PDF
    local_pdf = _download_pdf_to_temp(file_id, pdf_url, pdf_path)

    #Extraer texto
    text = extract_text_from_pdf_local(local_pdf)

    #Parsear a JSON
    offers = parse_offers(text)

    #Guardar JSON
    with open('ofertas.json', 'w', encoding='utf-8') as f:
        json.dump(offers, f, ensure_ascii=False, indent=2)

    #Devolver como string
    return json.dumps(offers, ensure_ascii=False)


#Assistants: tool handling **********************************************

def handle_tool_calls(client: OpenAI, thread_id: str, run):
    """
    Gestiona llamadas de herramientas. Actualmente solo 'scrape_jobs'.
    """
    if run.status != "requires_action":
        return run

    tool_outputs = []

    for tool_call in run.required_action.submit_tool_outputs.tool_calls:
        tool_call_id = tool_call.id
        name = tool_call.function.name
        args = json.loads(tool_call.function.arguments or "{}")

        if name == "scrape_jobs":
            try:
                output = scrape_jobs_tool(args)
            except Exception as e:
                output = json.dumps({"error": str(e)})
            tool_outputs.append({"tool_call_id": tool_call_id, "output": output})

        else:
            tool_outputs.append({
                "tool_call_id": tool_call_id,
                "output": json.dumps({"error": f"Herramienta no implementada: {name}"})
            })

    run = client.beta.threads.runs.submit_tool_outputs(
        thread_id=thread_id,
        run_id=run.id,
        tool_outputs=tool_outputs
    )
    return handle_tool_calls(client, thread_id, run)


def wait_for_run_completion(client: OpenAI, thread_id: str, run, sleep_interval: float = 2.0) -> str:
    """
    Espera a que termine el run. Resuelve herramientas intermedias.
    Devuelve el texto del último mensaje del asistente o un error.
    """
    while True:
        run = client.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run.id)

        if run.status in ["completed", "failed", "cancelled", "expired"]:
            if run.status == "completed":
                messages = client.beta.threads.messages.list(thread_id=thread_id)
                if not messages.data:
                    return "No hay mensajes en el hilo."
                last_message = messages.data[0]
                if not last_message.content:
                    return "El asistente no devolvió contenido."
                
                return last_message.content[0].text.value
            else:
                return f"Run terminado con estado: {run.status}"

        elif run.status == "requires_action":
            run = handle_tool_calls(client, thread_id, run)
            

        
        time.sleep(sleep_interval)


#Estado de sesión ***************************************************************************

if "thread_id" not in st.session_state:
    st.session_state.thread_id = None
if "messages" not in st.session_state:
    st.session_state.messages = []



# UI Sidebar ************************************************

with st.sidebar:
    st.header("⚙️ Fuente de ofertas (opcional)")
    st.text_input("Google Drive file ID", key="jobs_pdf_file_id", value=os.getenv("JOBS_PDF_FILE_ID", ""))
    st.text_input("PDF URL", key="jobs_pdf_url", value=os.getenv("JOBS_PDF_URL", ""))
    st.caption(
        "La tool `scrape_jobs` usará primero los argumentos que pida el asistente. "
        "Si no los pasa, tomará estos valores o, en su defecto, variables de entorno."
    )
    st.divider()
    st.markdown("**Assistant ID**")
    st.code(ASSISTANT_ID, language="text")


#UI principal

st.title("🔎 Asistente de Búsqueda de Trabajo")
st.write(
    "Este asistente analiza tu **CV**y, cuando se lo pidas, "
    "usa sus herramientas para recuperar ofertas desde un PDF y compararlas con tu perfil. "
    "No inventa datos: solo usa lo que figura en el CV."
)

with st.form(key="job_form"):
    user_msg = st.text_area("Tu mensaje para el asistente", height=120, placeholder="Ej.: Hola, revisa mi CV y pregúntame si quieres que busque ofertas.")
    submitted = st.form_submit_button("Enviar")

if submitted:
    if not user_msg.strip():
        st.error("Escribe un mensaje.")
    else:
        
        if st.session_state.thread_id is None:
            thread = client.beta.threads.create(messages=[{"role": "user", "content": user_msg.strip()}])
            st.session_state.thread_id = thread.id
        else:
            client.beta.threads.messages.create(
                thread_id=st.session_state.thread_id,
                role="user",
                content=user_msg.strip()
            )

        #
        run = client.beta.threads.runs.create(
            thread_id=st.session_state.thread_id,
            assistant_id=ASSISTANT_ID,
        )

        with st.spinner("Pensando..."):
            result_text = wait_for_run_completion(client, st.session_state.thread_id, run)

        
        st.session_state.messages.append({"user": user_msg.strip(), "assistant": result_text})

        
        st.write("### 🤖 Respuesta del asistente")
        st.markdown(result_text)

#Historial **************
if st.session_state.messages:
    st.write("### 📜 Historial de la conversación")
    for msg in st.session_state.messages:
        st.markdown(f"**Tú:** {msg['user']}")
        st.markdown(f"**Asistente:** {msg['assistant']}")
        st.markdown("---")
