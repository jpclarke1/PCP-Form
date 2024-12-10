from flask import Flask, Response, request, jsonify
from http.server import BaseHTTPRequestHandler
from datetime import datetime
import re
from PyPDF2 import PdfFileWriter, PdfFileReader
import io
import base64
import json

app = Flask(__name__)

def sanitize_filename(filename):
    """Sanitize filename to remove invalid characters."""
    invalid_chars = r'[<>:"/\\|?*\']'
    return re.sub(invalid_chars, '_', filename)

def extract_numeric_id(id_str):
    """Extract only numeric characters from an ID string."""
    return ''.join(filter(str.isdigit, str(id_str)))

def standardize_pcp_name(pcp_name):
    """Standardize PCP names to correct format."""
    if pcp_name.lower().strip() in ['de silva', 'dr de silva', 'dr. de silva']:
        return "Janesri De Silva M.D."
    return pcp_name

def normalize_note(note):
    """Normalize a note by removing timestamps, whitespace and common variations."""
    note = re.sub(r'\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}:\d{2}\s*(?:AM|PM)\s*>', '', note)
    note = re.sub(r'\s+', ' ', note)
    note = re.sub(r'(?:Eresh|Noah|Nisitha|Vihanga|Anchana)(?:\s+\w+)*', '', note)
    return note.strip()

def parse_pcp_note(note_text):
    """Parse PCP change note to extract relevant information."""
    info = {
        'new_pcp': '',
        'eff_date': '',
        'agent_name': '',
        'ref_number': ''
    }
    
    pcp_mappings = {
        'DE SILVA': "Janesri De Silva M.D.",
        'ARASTU': "Dr. Arastu",
        'JUAREZ MORALES': "Juarez Morales M.D.",
        'JUAREZ': "Juarez Morales M.D.",
        'RODRIGUEZ': "Barbara Rodriguez M.D.",
        'BHATT': "Brian Bhatt M.D.",
        'MINASSIAN': "Guiragos S. Minassian M.D.",
        'BENJAMIN': "Hilma R. Benjamin M.D.",
        'WOODS': "Marianne R. Woods M.D.",
        'SNYDER': "Mark Snyder M.D.",
        'FINEBERG': "Martin Fineberg M.D.",
        'BALA': "Padma Bala M.D.",
        'SHELAT': "Palak Shelat M.D.",
        'KEYNIGSHTEYN': "Rena Keynigshteyn M.D.",
        'MILLET': "Victoria E. Millet M.D.",
        'PARK': "Esther S. Park M.D.",
        'NAT': "Narindar K. Nat M.D.",
        'ZUNIGA': "Jocelyn C. Zuniga M.D.",
        'TAMASHIRO': "Victor G. Tamashiro M.D.",
        'BARBOUR': "Rachel Barbour M.D.",
        'ALTMAN': "Adrienne C. Altman M.D.",
        'UNGS': "Carolina M. Ungs M.D.",
        'BEHROOZAN': "Benjamin Behroozan M.D."
    }
    
    note_text_upper = note_text.upper()
    for pcp_name in pcp_mappings.keys():
        if pcp_name in note_text_upper:
            info['new_pcp'] = pcp_mappings[pcp_name]
            break
    
    eff_patterns = [
        r'EFF\s+DATE\s+RETRO\s+(\d{1,2}/\d{1,2}/\d{2,4})',
        r'EFFECTIVE\s+DATE\s+(\d{1,2}/\d{1,2}/\d{2,4})',
        r'EFFECTIVE\s+(\d{1,2}/\d{1,2}/\d{2,4})',
        r'EFF\s+DATE\s+(\d{1,2}/\d{1,2}/\d{2,4})',
        r'EFF\s+(\d{1,2}/?0?\d{1,2}/\d{2,4})',
        r'(\d{1,2}/\d{1,2}/\d{2,4})(?=\s*(?:LINDA|SHEILA|CIANNA|VANESSA|ROXY|MASIMBA|ANDREA|KARINA|$))'
    ]
    
    for pattern in eff_patterns:
        eff_match = re.search(pattern, note_text, re.IGNORECASE)
        if eff_match:
            date_str = eff_match.group(1).replace('0/', '/').replace('/0', '/')
            try:
                parsed_date = datetime.strptime(date_str, "%m/%d/%y" if len(date_str.split('/')[-1]) == 2 else "%m/%d/%Y")
                info['eff_date'] = parsed_date.strftime("%m/%d/%Y")
                break
            except ValueError:
                if not info['eff_date']:
                 end_date_match = re.search(r'\d{1,2}/\d{1,2}/\d{4}\s*[A-Z]+\s*$', note_text)
        if end_date_match:
            date_str = re.search(r'(\d{1,2}/\d{1,2}/\d{4})', end_date_match.group()).group(1)
            try:
                parsed_date = datetime.strptime(date_str, "%m/%d/%Y")
                info['eff_date'] = parsed_date.strftime("%m/%d/%Y")
            except ValueError:
                pass
                
    ref_patterns = [
        r'REF\s*#?\s*([A-Z0-9/-]+)',
        r'REF\.([A-Z0-9/-]+)',
        r'REF\s*\.\s*([A-Z0-9/-]+)',
        r'(?:^|\s)(?:REF|SFA)[-\s]*([A-Z0-9/-]+)',
        r'I-\d+'
    ]
    
    for pattern in ref_patterns:
        ref_match = re.search(pattern, note_text)
        if ref_match:
            info['ref_number'] = ref_match.group(1)
            break
    
    if 'GALVEZ' in note_text.upper() and 'NANCY' in note_text.upper():
        info['agent_name'] = "Nancy Galvez"
    else:
        name_pattern = r'([A-Za-z]+),\s*([A-Za-z]+)(?=\s+\d{2}/\d{2}/\d{4})'
        name_match = re.search(name_pattern, note_text)
        if name_match:
            last_name, first_name = name_match.groups()
            info['agent_name'] = f"{first_name.strip()} {last_name.strip()}"
    
    return info

def extract_patient_data(row):
    """Extract patient data from a single row."""
    columns = row.split('\t')
    if len(columns) < 7:
        return None
        
    try:
        ktmg_id = extract_numeric_id(columns[1])
        old_pcp_field = columns[4].split('/')[0].strip() if '/' in columns[4] else columns[4].strip()
        
        patient = {
            'date': columns[0].strip(),
            'ktmg_id': ktmg_id,
            'dob': columns[2].strip(),
            'patient_name': columns[3].split(' DHS ')[0].strip() if ' DHS ' in columns[3] else columns[3].strip(),
            'old_pcp': old_pcp_field,
            'member_id': columns[5].strip(),
            'phone': columns[6].strip()
        }
        
        member_id_match = re.search(r'\*\*([0-9A-Z]+)\*\*', patient['member_id'])
        if member_id_match:
            patient['member_id'] = member_id_match.group(1)
        
        stripped_number = re.sub(r'\D', '', patient['phone'])
        if len(stripped_number) == 10:
            patient['phone'] = f"{stripped_number[:3]}-{stripped_number[3:6]}-{stripped_number[6:]}"
        
        try:
            patient['date'] = datetime.strptime(patient['date'], "%m/%d/%Y").strftime("%m/%d/%Y")
        except ValueError:
            patient['date'] = ''
            
        try:
            patient['dob'] = datetime.strptime(patient['dob'], "%m/%d/%Y").strftime("%m/%d/%Y")
        except ValueError:
            patient['dob'] = ''
            
        return patient
    except Exception as e:
        print(f"Error processing row: {str(e)}")
        return None

def parse_input(text):
    """Parse the entire input text and match notes with patient data."""
    lines = [line.strip() for line in text.split('\n') if line.strip()]
    
    notes = []
    patients = []
    seen_notes = {}
    
    for line in lines:
        note_patterns = [
            'PCP CHANGE TO',
            'PCP WAS MADE TO',
            'PCP WAS CHANGE TO',
            'PCP CHANGE WAS MADE TO',
            'CHANGE TO DR',
            'DR DE SILVA',
            'DR. DE SILVA',
            'DR BEHROOZAN',
            'DR. BEHROOZAN',
            'DR JUAREZ',
            'DR. JUAREZ',
            'TRANSFER WAS MADE',
            'TRANSFER WAS DONE'
        ]
        
        if (any(pattern in line.upper() for pattern in note_patterns) and 
            ('PM >' in line or 'AM >' in line or 'PM>' in line or 'AM>' in line)):
            normalized_note = normalize_note(line)
            if normalized_note:
                if normalized_note not in seen_notes:
                    seen_notes[normalized_note] = line
                    notes.append(line)
        elif '\t' in line and not any(name in line for name in ['Eresh', 'Noah', 'Nisitha', 'Vihanga', 'Anchana']):
            patient = extract_patient_data(line)
            if patient:
                patients.append(patient)
    
    if len(notes) > 0 and len(patients) > 0:
        unique_notes = list(dict.fromkeys(notes))
        pairs = []
        for i in range(min(len(unique_notes), len(patients))):
            patient = patients[i].copy()
            patient['note'] = unique_notes[i]
            pairs.append(patient)
        return pairs
    
    return []

def handle_request(request):
    if request.method == 'POST':
        try:
            # Your existing processing logic here
            return jsonify({'message': 'Success'})
        except Exception as e:
            return jsonify({'error': str(e)}), 500
    return jsonify({'error': 'Method not allowed'}), 405

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({'message': 'Please use POST method'}).encode())
        
    def do_POST(self):
        content_length = int(self.headers.get('Content-Length', 0))
        post_data = self.rfile.read(content_length)
        
        with app.test_request_context(
            path='/',
            method='POST',
            input_stream=io.BytesIO(post_data),
            content_length=content_length,
            content_type=self.headers.get('Content-Type', 'application/x-www-form-urlencoded')
        ):
            response = handle_request(request)
            
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(response).encode())

def lambda_handler(event, context):
    return Handler()