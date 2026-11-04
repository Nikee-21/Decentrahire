"""
DecentraHire - Flask Application
Main entry point for the AI-powered recruitment platform.
"""

import os
import datetime
import random
import io
import json
from dotenv import load_dotenv

load_dotenv()

try:
    import PyPDF2
except ImportError:
    PyPDF2 = None
from flask import Flask, render_template, request, jsonify, session
from flask_cors import CORS
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from pymongo import MongoClient

from modules.resume_scanner import scan_resume
from modules.audio_transcriber import transcribe_audio
from modules.certificate_scanner import scan_certificate
from modules.digilocker_verifier import DigiLockerClient
from modules.verification_engine import compare_metadata, STATUS_NO_DOC_SELECTED
from modules.interview_practice import (
    generate_questions_with_gemini,
    review_answer_with_gemini,
    generate_final_report,
)
from modules.email_service import send_otp_email, send_status_email
from modules.test_parser import extract_text_from_file, parse_questions

def check_digilocker(file_bytes, filename):
    text = ""
    ext = filename.lower().rsplit('.', 1)[-1] if '.' in filename else ''
    if ext == 'pdf' and PyPDF2 is not None:
        try:
            reader = PyPDF2.PdfReader(io.BytesIO(file_bytes))
            for page in reader.pages:
                extracted = page.extract_text()
                if extracted:
                    text += extracted + "\n"
        except Exception as e:
            print(f"PDF extraction error: {e}")
    elif ext in ['png', 'jpg', 'jpeg', 'bmp', 'tiff', 'tif', 'webp']:
        from modules.certificate_scanner import extract_text_ocr
        text = extract_text_ocr(file_bytes)
    return "digilocker" in text.lower()

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'decentrahire-super-secret')
CORS(app)  # Allow cross-origin requests from Live Server
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB max
app.config['UPLOAD_FOLDER'] = os.path.join(os.path.dirname(__file__), 'static', 'uploads')

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# MongoDB Configuration
MONGO_URI = os.getenv("MONGO_URI")
try:
    mongo_client = MongoClient(MONGO_URI)
    db = mongo_client['decentrahire']
    users_collection = db['users']
    recruiter_collection = db['recruiter']
    documents_collection = db['documents']
    otp_sessions = db['otp_sessions']
    temp_users = db['temp_users']
    applications_collection = db['recruiter_applications']
    candidate_applications = db['candidate_applications']
    tests_collection = db['tests']
except Exception as e:
    print(f"MongoDB connection error: {e}")


# DigiLocker client (singleton)
digilocker_client = DigiLockerClient()


@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/register/step1', methods=['POST'])
def api_register_step1():
    email = request.form.get('email', '').strip()
    if not email:
        return jsonify({"error": "Email is required"}), 400
        
    if users_collection.find_one({"email": email}) or recruiter_collection.find_one({"email": email}):
        return jsonify({"error": "Email already registered"}), 400
        
    # Collect all step 1 fields
    temp_data = {
        "email": email,
        "full_name": request.form.get('full_name', '').strip(),
        "dob": request.form.get('dob', '').strip(),
        "gender": request.form.get('gender', '').strip(),
        "phone": request.form.get('phone', '').strip(),
        "current_address": request.form.get('current_address', '').strip(),
        "permanent_address": request.form.get('permanent_address', '').strip(),
        "password": request.form.get('password', ''),
        "role": request.form.get('role', 'candidate'),
        "created_at": datetime.datetime.utcnow().isoformat()
    }
    
    # Save profile photo if provided
    if 'profile_photo' in request.files:
        photo = request.files['profile_photo']
        if photo.filename != '':
            filename = secure_filename(photo.filename)
            photo_path = os.path.join(app.config['UPLOAD_FOLDER'], f"photo_{email}_{filename}")
            photo.save(photo_path)
            temp_data['profile_photo_path'] = f"/static/uploads/photo_{email}_{filename}"

    temp_users.update_one({"email": email}, {"$set": temp_data}, upsert=True)
    
    # Generate OTP
    otp_code = str(random.randint(100000, 999999))
    print(f"\n[OTP DEBUG] Generated OTP for {email}: {otp_code}\n")
    otp_sessions.update_one(
        {"email": email}, 
        {"$set": {"otp": otp_code, "created_at": datetime.datetime.utcnow().isoformat()}}, 
        upsert=True
    )
    
    # Send email
    result = send_otp_email(email, otp_code, temp_data["full_name"])
    if not result.get("success"):
        return jsonify({"error": f"Failed to send OTP email: {result.get('error', 'Unknown')}"}), 500
        
    return jsonify({"success": True, "message": "OTP sent successfully"})

@app.route('/api/register/verify-otp', methods=['POST'])
def api_verify_otp():
    data = request.get_json(silent=True) or {}
    email = data.get('email', '').strip()
    otp_input = data.get('otp', '').strip()
    
    session_data = otp_sessions.find_one({"email": email})
    expected_otp = session_data.get('otp') if session_data else "None"
    print(f"\n[OTP DEBUG] Verifying OTP for {email}: Entered={otp_input}, Expected={expected_otp}\n")
    
    if not session_data or session_data.get('otp') != otp_input:
        return jsonify({"error": "Invalid OTP or session expired"}), 400
        
    # Mark as verified
    temp_users.update_one({"email": email}, {"$set": {"email_verified": True}})
    
    return jsonify({"success": True, "message": "Email verified successfully"})

@app.route('/api/register/step3', methods=['POST'])
def api_register_step3():
    email = request.form.get('email', '').strip()
    temp_user = temp_users.find_one({"email": email, "email_verified": True})
    
    if not temp_user:
        return jsonify({"error": "User session not found or email not verified"}), 400
        
    role = temp_user.get("role", "candidate")
    
    if role == 'interviewer':
        # Retrieve company details from request
        company_name = request.form.get('company_name', '').strip()
        company_industry = request.form.get('company_industry', '').strip()
        company_size = request.form.get('company_size', '').strip()
        company_location = request.form.get('company_location', '').strip()
        company_description = request.form.get('company_description', '').strip()
        
        # Save company logo if provided
        company_logo_path = ""
        if 'company_logo' in request.files:
            logo_file = request.files['company_logo']
            if logo_file.filename != '':
                filename = secure_filename(logo_file.filename)
                logo_full_path = os.path.join(app.config['UPLOAD_FOLDER'], f"logo_{email}_{filename}")
                logo_file.save(logo_full_path)
                company_logo_path = f"/static/uploads/logo_{email}_{filename}"
                
        # Finalize interviewer user creation
        hashed_password = generate_password_hash(temp_user.get('password', ''))
        final_user = {
            "username": temp_user.get("full_name", "user").split()[0] + str(random.randint(10,99)),
            "full_name": temp_user.get("full_name"),
            "email": email,
            "mobile": temp_user.get("phone"),
            "password": hashed_password,
            "role": "interviewer",
            "profile_photo_path": temp_user.get("profile_photo_path", ""),
            "company_name": company_name,
            "company_industry": company_industry,
            "company_size": company_size,
            "company_location": company_location,
            "company_description": company_description,
            "company_logo_path": company_logo_path
        }
    else:
        # Process Education Data (passed as JSON string)
        education_data = request.form.get('education_data', '[]')
        try:
            education_list = json.loads(education_data)
        except Exception:
            education_list = []
            
        # Process Documents
        doc_keys = ['resume', '10th_marksheet', '12th_marksheet', 'graduation_cert', 'additional_certs', 'gov_id']
        saved_docs = []
        
        for key in request.files:
            if key in doc_keys or key.startswith('additional_certs'):
                file = request.files[key]
                if file.filename != '':
                    file_bytes = file.read()
                    file.seek(0)
                    
                    # Verify DigiLocker sign, skipping the resume, 10th, and 12th marksheets
                    if key not in ['resume', '10th_marksheet', '12th_marksheet']:
                        is_digilocker = check_digilocker(file_bytes, file.filename)
                        if not is_digilocker:
                            return jsonify({"error": f"Document '{file.filename}' rejected: No 'DigiLocker' mark found."}), 400
                        
                    filename = secure_filename(file.filename)
                    file_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{key}_{email}_{filename}")
                    file.save(file_path)
                    
                    doc_record = {
                        "user_email": email,
                        "document_type": key,
                        "file_name": filename,
                        "file_path": f"/static/uploads/{key}_{email}_{filename}",
                        "upload_timestamp": datetime.datetime.utcnow().isoformat()
                    }
                    documents_collection.insert_one(doc_record)
                    saved_docs.append(doc_record['file_path'])
                    
        # Finalize candidate user creation
        hashed_password = generate_password_hash(temp_user.get('password', ''))
        final_user = {
            "username": temp_user.get("full_name", "user").split()[0] + str(random.randint(10,99)),
            "full_name": temp_user.get("full_name"),
            "dob": temp_user.get("dob"),
            "gender": temp_user.get("gender"),
            "email": email,
            "mobile": temp_user.get("phone"),
            "current_address": temp_user.get("current_address"),
            "permanent_address": temp_user.get("permanent_address"),
            "password": hashed_password,
            "role": "candidate",
            "profile_photo_path": temp_user.get("profile_photo_path", ""),
            "education": education_list,
            "documents": saved_docs
        }
    
    if role == 'interviewer':
        recruiter_collection.insert_one(final_user)
    else:
        users_collection.insert_one(final_user)
    
    # Clean up temp data
    temp_users.delete_one({"email": email})
    otp_sessions.delete_one({"email": email})
    
    final_user.pop('_id', None)
    final_user.pop('password', None)
    
    return jsonify({"success": True, "message": "Registration finalized successfully", "user": final_user}), 201

@app.route('/api/login', methods=['POST'])
def api_login():
    """Endpoint: Login a user."""
    data = request.get_json(silent=True) or {}
    email = data.get('email', '').strip()
    password = data.get('password', '')

    if not email or not password:
        return jsonify({"error": "Email and Password are required"}), 400

    user = users_collection.find_one({"email": email})
    if not user:
        user = recruiter_collection.find_one({"email": email})
        
    if not user or not check_password_hash(user['password'], password):
        return jsonify({"error": "Invalid email or password"}), 401

    return jsonify({
        "success": True,
        "message": "Login successful",
        "user": {
            "name": user.get('full_name'),
            "username": user.get('username', ''),
            "email": user.get('email'),
            "role": user.get('role'),
            "dob": user.get('dob', ''),
            "gender": user.get('gender', ''),
            "permanent_address": user.get('permanent_address', ''),
            "profile_photo_path": user.get('profile_photo_path', ''),
            "phone": user.get('mobile', ''),
            "location": user.get('current_address', ''),
            "bio": user.get('bio', ''),
            "skills": user.get('skills', ''),
            "experience": user.get('experience', ''),
            "education_str": user.get('education_str', ''),
            "linkedin": user.get('linkedin', ''),
            "company_name": user.get('company_name', ''),
            "company_industry": user.get('company_industry', ''),
            "company_size": user.get('company_size', ''),
            "company_location": user.get('company_location', ''),
            "company_description": user.get('company_description', ''),
            "company_logo_path": user.get('company_logo_path', '')
        }
    })

@app.route('/api/profile/update', methods=['POST'])
def api_profile_update():
    """Endpoint: Update user profile."""
    email = request.form.get('email', '').strip()
    if not email:
        return jsonify({"error": "Email is required"}), 400
        
    update_fields = {
        "full_name": request.form.get('name'),
        "mobile": request.form.get('phone'),
        "current_address": request.form.get('location'),
        "permanent_address": request.form.get('permanent_address'),
        "dob": request.form.get('dob'),
        "gender": request.form.get('gender'),
        "bio": request.form.get('bio'),
        "skills": request.form.get('skills'),
        "experience": request.form.get('experience'),
        "education_str": request.form.get('education'),
        "linkedin": request.form.get('linkedin')
    }
    
    # Recruiter company profile fields
    if request.form.get('company_name') is not None:
        update_fields["company_name"] = request.form.get('company_name').strip()
    if request.form.get('company_industry') is not None:
        update_fields["company_industry"] = request.form.get('company_industry').strip()
    if request.form.get('company_size') is not None:
        update_fields["company_size"] = request.form.get('company_size').strip()
    if request.form.get('company_location') is not None:
        update_fields["company_location"] = request.form.get('company_location').strip()
    if request.form.get('company_description') is not None:
        update_fields["company_description"] = request.form.get('company_description').strip()
        
    # Save profile photo if provided
    if 'profile_photo' in request.files:
        photo = request.files['profile_photo']
        if photo.filename != '':
            filename = secure_filename(photo.filename)
            photo_path = os.path.join(app.config['UPLOAD_FOLDER'], f"photo_{email}_{filename}")
            photo.save(photo_path)
            update_fields['profile_photo_path'] = f"/static/uploads/photo_{email}_{filename}"

    # Save company logo if provided
    if 'company_logo' in request.files:
        logo_file = request.files['company_logo']
        if logo_file.filename != '':
            filename = secure_filename(logo_file.filename)
            logo_path = os.path.join(app.config['UPLOAD_FOLDER'], f"logo_{email}_{filename}")
            logo_file.save(logo_path)
            update_fields['company_logo_path'] = f"/static/uploads/logo_{email}_{filename}"
    
    # Remove None values
    update_fields = {k: v for k, v in update_fields.items() if v is not None}
    
    user = users_collection.find_one({"email": email})
    if user:
        users_collection.update_one({"email": email}, {"$set": update_fields})
        user = users_collection.find_one({"email": email})
    else:
        recruiter_collection.update_one({"email": email}, {"$set": update_fields})
        user = recruiter_collection.find_one({"email": email})
    return jsonify({
        "success": True,
        "message": "Profile updated",
        "user": {
            "name": user.get('full_name'),
            "username": user.get('username', ''),
            "email": user.get('email'),
            "role": user.get('role'),
            "dob": user.get('dob', ''),
            "gender": user.get('gender', ''),
            "permanent_address": user.get('permanent_address', ''),
            "profile_photo_path": user.get('profile_photo_path', ''),
            "phone": user.get('mobile', ''),
            "location": user.get('current_address', ''),
            "bio": user.get('bio', ''),
            "skills": user.get('skills', ''),
            "experience": user.get('experience', ''),
            "education_str": user.get('education_str', ''),
            "linkedin": user.get('linkedin', ''),
            "company_name": user.get('company_name', ''),
            "company_industry": user.get('company_industry', ''),
            "company_size": user.get('company_size', ''),
            "company_location": user.get('company_location', ''),
            "company_description": user.get('company_description', ''),
            "company_logo_path": user.get('company_logo_path', '')
        }
    })

@app.route('/api/documents', methods=['POST'])
def api_upload_document():
    """Endpoint: Upload and save a document for a user."""
    email = request.form.get('email', '').strip()
    document_type = request.form.get('document_type', 'general')
    
    if not email:
        return jsonify({"error": "User email is required"}), 400
        
    if 'file' not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
        
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No file selected"}), 400
        
    filename = secure_filename(file.filename)
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{document_type}_{email}_{filename}")
    
    # DigiLocker verification for non-resume documents
    if document_type != 'resume':
        from modules.certificate_scanner import scan_certificate
        file_bytes = file.read()
        file.seek(0)
        
        # Determine if it's an image. If it's PDF, we can't easily OCR without pdf2image, but the prompt says OCR is used.
        # Actually our certificate_scanner handles images.
        ext = filename.lower().rsplit('.', 1)[-1] if '.' in filename else ''
        if ext not in ('png', 'jpg', 'jpeg', 'bmp', 'tiff', 'tif', 'webp'):
            return jsonify({"error": "For certificates, please upload an image file (PNG/JPG) so it can be DigiLocker verified."}), 400
            
        ocr_result = scan_certificate(file_bytes, filename)
        if not ocr_result.get("is_digilocker_verified"):
            return jsonify({"error": "Validation Failed: This document is missing the DigiLocker verification symbol or digital signature. Please upload a valid DigiLocker document."}), 400

    try:
        file.save(file_path)
        
        # If it's a unique document type like resume, replace the old one
        unique_types = ['resume', 'gov_id', '10th_marksheet', '12th_marksheet', 'graduation_cert']
        if document_type in unique_types:
            documents_collection.delete_many({"user_email": email, "document_type": document_type})
        
        doc_data = {
            "user_email": email,
            "document_type": document_type,
            "file_name": filename,
            "file_path": f"/static/uploads/{document_type}_{email}_{filename}",
            "upload_timestamp": datetime.datetime.utcnow().isoformat()
        }
        
        documents_collection.insert_one(doc_data)
        doc_data.pop('_id', None)
        
        return jsonify({"success": True, "message": "Document uploaded successfully", "document": doc_data}), 201
    except Exception as e:
        return jsonify({"error": f"Failed to upload document: {str(e)}"}), 500

@app.route('/api/documents', methods=['GET'])
def api_get_documents():
    """Endpoint: Get all documents for a user."""
    email = request.args.get('email', '').strip()
    
    if not email:
        return jsonify({"error": "User email is required"}), 400
        
    try:
        documents = list(documents_collection.find({"user_email": email}))
        for doc in documents:
            doc['_id'] = str(doc['_id'])
            
        return jsonify({"success": True, "documents": documents}), 200
    except Exception as e:
        return jsonify({"error": f"Failed to retrieve documents: {str(e)}"}), 500

@app.route('/api/scan-resume', methods=['POST'])
def api_scan_resume():
    """Endpoint: Upload resume + job description, get analysis."""
    if 'file' not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No file selected"}), 400
    
    job_description = request.form.get('job_description', '')
    
    try:
        file_bytes = file.read()
        filename = secure_filename(file.filename)
        result = scan_resume(file_bytes, filename, job_description)
        
        if "error" in result:
            return jsonify(result), 400
        
        return jsonify(result)
    
    except Exception as e:
        return jsonify({"error": f"Processing failed: {str(e)}"}), 500

@app.route('/api/candidate/scan-resume', methods=['POST'])
def api_candidate_scan_resume():
    """Endpoint: Scan the candidate's existing resume from the database."""
    data = request.get_json(silent=True) or {}
    email = data.get('email', '').strip()
    job_description = data.get('job_description', '')
    
    if not email:
        return jsonify({"error": "Email is required"}), 400
        
    doc = documents_collection.find_one({"user_email": email, "document_type": "resume"})
    if not doc:
        return jsonify({"error": "No resume found. Please ensure you uploaded your resume during registration."}), 404
        
    # Construct absolute path to the file
    # file_path is stored as '/static/uploads/...'
    relative_path = doc['file_path'].lstrip('/')
    abs_file_path = os.path.join(app.root_path, relative_path)
    
    try:
        with open(abs_file_path, 'rb') as f:
            file_bytes = f.read()
    except Exception as e:
        return jsonify({"error": f"Failed to read resume file: {str(e)}"}), 500
        
    result = scan_resume(file_bytes, doc['file_name'], job_description)
    
    if "error" in result:
        return jsonify(result), 400
        
    # Calculate improvements and ATS feedback
    score = result.get("match_score", 0)
    improvements = []
    
    if score < 50:
        improvements.append("Your resume lacks keywords. Add more technical and soft skills.")
        improvements.append("Highlight specific achievements with quantifiable metrics.")
        improvements.append("Ensure you have clearly marked Education and Experience sections.")
    elif score < 75:
        improvements.append("Good start! Add more industry-standard technical skills to boost your score.")
        improvements.append("Make sure your years of experience are clearly formatted (e.g. '3+ years experience').")
        
    if len(result.get("skills", {}).get("technical", [])) < 5:
        improvements.append("Consider adding a dedicated 'Technical Skills' section to make keywords easier for ATS to parse.")
        
    if not result.get("education", []):
        improvements.append("ATS could not detect your Education. Use clear headers like 'Education' or 'Academic Qualifications'.")
        
    result["improvements"] = improvements
    result["is_good"] = score >= 75
        
    return jsonify(result)


@app.route('/api/transcribe-audio', methods=['POST'])
def api_transcribe_audio():
    """Endpoint: Upload audio file, get transcript + analysis."""
    if 'file' not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No file selected"}), 400
    
    try:
        file_bytes = file.read()
        filename = secure_filename(file.filename)
        result = transcribe_audio(file_bytes, filename)
        
        if "error" in result:
            return jsonify(result), 400 if not result.get("success", True) else 200
        
        return jsonify(result)
    
    except Exception as e:
        return jsonify({"error": f"Transcription failed: {str(e)}"}), 500


@app.route('/api/scan-certificate', methods=['POST'])
def api_scan_certificate():
    """Endpoint: Upload certificate image, get extracted data."""
    if 'file' not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No file selected"}), 400
    
    try:
        file_bytes = file.read()
        filename = secure_filename(file.filename)
        result = scan_certificate(file_bytes, filename)
        
        if "error" in result:
            return jsonify(result), 400
        
        return jsonify(result)
    
    except Exception as e:
        return jsonify({"error": f"Certificate scanning failed: {str(e)}"}), 500


@app.route('/api/digilocker-connect', methods=['POST'])
def api_digilocker_connect():
    """Endpoint: Connect to DigiLocker (simulated OAuth) and fetch user documents."""
    data = request.get_json(silent=True) or {}
    email = data.get('email', '').strip()

    if not email:
        return jsonify({"error": "Email address is required"}), 400

    try:
        # Simulate OAuth
        auth_result = digilocker_client.simulate_oauth(email)
        if not auth_result.get("success"):
            return jsonify(auth_result), 404

        # Fetch user's documents
        docs_result = digilocker_client.fetch_user_documents(email)

        return jsonify({
            "success": True,
            "session_token": auth_result.get("session_token"),
            "message": auth_result.get("message"),
            "documents": docs_result.get("documents", []),
            "document_count": docs_result.get("document_count", 0),
        })

    except Exception as e:
        return jsonify({"error": f"DigiLocker connection failed: {str(e)}"}), 500


@app.route('/api/verify-digilocker', methods=['POST'])
def api_verify_digilocker():
    """
    Endpoint: Verify a certificate against DigiLocker.

    PRIMARY method (realistic flow):
      - digilocker_email + doc_index → fetch user's documents → pick one → compare

    FALLBACK method (deprecated — URI does not exist in real DigiLocker):
      - digilocker_uri → loose match attempt → compare with lower confidence
    """
    if 'file' not in request.files:
        return jsonify({"error": "No certificate file uploaded"}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No file selected"}), 400

    digilocker_email = request.form.get('digilocker_email', '').strip()
    doc_index = request.form.get('doc_index', '')
    digilocker_uri = request.form.get('digilocker_uri', '').strip()

    try:
        # Step 1: OCR scan the uploaded certificate
        file_bytes = file.read()
        filename = secure_filename(file.filename)
        ocr_result = scan_certificate(file_bytes, filename)

        if not ocr_result.get("success"):
            return jsonify({
                "error": ocr_result.get("error", "OCR extraction failed"),
                "success": False,
            }), 400

        dl_document = None
        match_method = "none"

        # ── PRIMARY: email-based flow (realistic DigiLocker) ──
        if digilocker_email:
            docs_result = digilocker_client.fetch_user_documents(digilocker_email)

            if not docs_result.get("success") or not docs_result.get("documents"):
                return jsonify({
                    "error": f"No DigiLocker documents found for {digilocker_email}",
                    "success": False,
                    "ocr_data": ocr_result,
                }), 404

            documents = docs_result["documents"]

            # Require a valid document selection
            if doc_index == '' or not doc_index.strip().isdigit():
                return jsonify({
                    "error": STATUS_NO_DOC_SELECTED,
                    "success": False,
                    "status": STATUS_NO_DOC_SELECTED,
                    "documents": documents,
                    "ocr_data": ocr_result,
                }), 400

            idx = int(doc_index)
            if idx < 0 or idx >= len(documents):
                return jsonify({
                    "error": f"Invalid document index: {idx}. Available: 0–{len(documents)-1}",
                    "success": False,
                    "ocr_data": ocr_result,
                }), 400

            dl_document = documents[idx]
            match_method = "account"

        # ── FALLBACK: URI-based flow (deprecated — loose matching) ──
        elif digilocker_uri:
            uri_result = digilocker_client.loose_match_by_uri(digilocker_uri)

            if not uri_result.get("success"):
                return jsonify({
                    "error": uri_result.get("error", "DigiLocker document not found"),
                    "success": False,
                    "ocr_data": ocr_result,
                }), 404

            dl_document = uri_result["document"]
            match_method = f"uri_{uri_result.get('match_type', 'unknown')}"

        # ── NEITHER provided ──
        else:
            return jsonify({
                "error": STATUS_NO_DOC_SELECTED,
                "success": False,
                "status": STATUS_NO_DOC_SELECTED,
            }), 400

        # Step 3: Compare OCR data with DigiLocker metadata
        verification = compare_metadata(ocr_result, dl_document)

        return jsonify({
            "success": True,
            "match_method": match_method,
            "verification": verification,
            "ocr_data": {
                "candidate_name": ocr_result.get("candidate_name"),
                "certification_title": ocr_result.get("certification_title"),
                "issuing_authority": ocr_result.get("issuing_authority"),
                "dates": ocr_result.get("dates", []),
                "confidence": ocr_result.get("confidence"),
            },
            "digilocker_data": dl_document,
        })

    except Exception as e:
        return jsonify({"error": f"Verification failed: {str(e)}"}), 500


# ============ MockVue Interview Practice API ============

@app.route('/api/mockvue/questions', methods=['POST'])
def api_mockvue_questions():
    """Generate interview questions using Gemini AI."""
    data = request.get_json(silent=True) or {}
    role = data.get('role', 'Software Engineer')
    interview_type = data.get('interview_type', 'general')
    num_questions = min(int(data.get('num_questions', 5)), 10)
    difficulty = data.get('difficulty', 'mixed')

    try:
        result = generate_questions_with_gemini(
            role=role,
            interview_type=interview_type,
            num_questions=num_questions,
            difficulty=difficulty,
        )
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": f"Question generation failed: {str(e)}"}), 500


@app.route('/api/mockvue/review', methods=['POST'])
def api_mockvue_review():
    """Review a single answer using Gemini AI."""
    data = request.get_json(silent=True) or {}
    question = data.get('question', '')
    answer = data.get('answer', '')
    role = data.get('role', 'Software Engineer')
    movement_data = data.get('movement_data', None)

    if not question:
        return jsonify({"error": "No question provided"}), 400

    try:
        result = review_answer_with_gemini(
            question=question,
            answer=answer,
            role=role,
            movement_data=movement_data,
        )
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": f"Review failed: {str(e)}"}), 500


@app.route('/api/mockvue/report', methods=['POST'])
def api_mockvue_report():
    """Generate final comprehensive interview report."""
    data = request.get_json(silent=True) or {}

    try:
        result = generate_final_report(data)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": f"Report generation failed: {str(e)}"}), 500


@app.route('/api/mockvue/transcribe', methods=['POST'])
def api_mockvue_transcribe():
    """Transcribe audio from interview practice using Whisper/SR."""
    if 'file' not in request.files:
        return jsonify({"error": "No audio file uploaded"}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No file selected"}), 400

    try:
        file_bytes = file.read()
        filename = secure_filename(file.filename)
        result = transcribe_audio(file_bytes, filename)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": f"Transcription failed: {str(e)}"}), 500


@app.route('/api/applications/create', methods=['POST'])
def api_create_application():
    """Endpoint: Create a new job application (Interviewer/Recruiter only)."""
    data = request.get_json(silent=True) or {}
    
    recruiter_email = data.get('recruiter_email', '').strip()
    if not recruiter_email:
        return jsonify({"error": "Recruiter email is required"}), 400
        
    # Verify recruiter exists
    recruiter = recruiter_collection.find_one({"email": recruiter_email})
    if not recruiter:
        return jsonify({"error": "Recruiter profile not found. Please log in again."}), 404

    # Extract fields
    job_title = data.get('job_title', '').strip()
    domain = data.get('domain', '').strip()
    job_type = data.get('job_type', '').strip()
    job_location = data.get('job_location', '').strip()
    openings = data.get('openings')
    deadline = data.get('deadline', '').strip()
    salary_min = data.get('salary_min')
    salary_max = data.get('salary_max')
    min_education = data.get('min_education', '').strip()
    min_experience = data.get('min_experience')
    required_skills_str = data.get('required_skills', '').strip()
    job_description = data.get('job_description', '').strip()
    interview_type = data.get('interview_type', '').strip()
    num_questions = data.get('num_questions')
    time_limit = data.get('time_limit')

    # Validation
    required_str_fields = [
        ("Job title", job_title),
        ("Domain", domain),
        ("Job type", job_type),
        ("Job location", job_location),
        ("Deadline", deadline),
        ("Minimum education", min_education),
        ("Required skills", required_skills_str),
        ("Job description", job_description),
        ("Interview type", interview_type)
    ]
    for field_name, value in required_str_fields:
        if not value:
            return jsonify({"error": f"{field_name} is required"}), 400

    # Type-cast numbers and validate
    try:
        openings = int(openings)
        if openings < 1:
            return jsonify({"error": "Number of openings must be at least 1"}), 400
    except (TypeError, ValueError):
        return jsonify({"error": "Openings must be a valid integer"}), 400

    try:
        salary_min = int(salary_min)
        salary_max = int(salary_max)
        if salary_min < 0 or salary_max < 0:
            return jsonify({"error": "Salary range values must be non-negative"}), 400
        if salary_min > salary_max:
            return jsonify({"error": "Minimum salary cannot be greater than maximum salary"}), 400
    except (TypeError, ValueError):
        return jsonify({"error": "Salary values must be valid integers"}), 400

    try:
        min_experience = int(min_experience)
        if min_experience < 0:
            return jsonify({"error": "Minimum experience cannot be negative"}), 400
    except (TypeError, ValueError):
        return jsonify({"error": "Minimum experience must be a valid integer"}), 400

    try:
        num_questions = int(num_questions)
        if not (1 <= num_questions <= 20):
            return jsonify({"error": "Number of questions must be between 1 and 20"}), 400
    except (TypeError, ValueError):
        return jsonify({"error": "Number of questions must be a valid integer"}), 400

    try:
        time_limit = int(time_limit)
        if not (5 <= time_limit <= 180):
            return jsonify({"error": "Time limit must be between 5 and 180 minutes"}), 400
    except (TypeError, ValueError):
        return jsonify({"error": "Time limit must be a valid integer"}), 400

    # Parse skills to array
    skills_list = [s.strip() for s in required_skills_str.split(',') if s.strip()]
    if not skills_list:
        return jsonify({"error": "At least one skill must be provided"}), 400

    # Enrich from recruiter profile
    company_name = recruiter.get('company_name', 'Acme Corp')
    company_logo_path = recruiter.get('company_logo_path', '')

    new_app = {
        "recruiter_email": recruiter_email,
        "company_name": company_name,
        "company_logo_path": company_logo_path,
        "job_title": job_title,
        "domain": domain,
        "job_type": job_type,
        "job_location": job_location,
        "openings": openings,
        "deadline": deadline,
        "salary_range": {
            "min": salary_min,
            "max": salary_max
        },
        "min_education": min_education,
        "min_experience": min_experience,
        "required_skills": skills_list,
        "job_description": job_description,
        "interview_type": interview_type,
        "num_questions": num_questions,
        "time_limit": time_limit,
        "created_at": datetime.datetime.utcnow().isoformat()
    }

    try:
        res = applications_collection.insert_one(new_app)
        new_app['_id'] = str(res.inserted_id)
        return jsonify({"success": True, "application": new_app}), 201
    except Exception as e:
        return jsonify({"error": f"Failed to save application: {str(e)}"}), 500


@app.route('/api/applications/list', methods=['GET'])
def api_list_applications():
    """List all job applications posted by a recruiter."""
    email = request.args.get('email', '').strip()
    if not email:
        return jsonify({"error": "Recruiter email is required"}), 400
        
    try:
        apps = list(applications_collection.find({"recruiter_email": email}).sort("created_at", -1))
        for a in apps:
            a['_id'] = str(a['_id'])
        return jsonify({"success": True, "applications": apps})
    except Exception as e:
        return jsonify({"error": f"Failed to fetch applications: {str(e)}"}), 500


@app.route('/api/applications/update', methods=['POST'])
def api_update_application():
    """Endpoint: Update an existing job application (Interviewer/Recruiter only)."""
    data = request.get_json(silent=True) or {}
    
    app_id = data.get('id', '').strip()
    recruiter_email = data.get('recruiter_email', '').strip()
    if not app_id or not recruiter_email:
        return jsonify({"error": "Application ID and Recruiter email are required"}), 400
        
    from bson.objectid import ObjectId
    try:
        app_obj_id = ObjectId(app_id)
    except Exception:
        return jsonify({"error": "Invalid application ID format"}), 400
        
    application = applications_collection.find_one({"_id": app_obj_id})
    if not application:
        return jsonify({"error": "Application not found"}), 404
        
    if application.get('recruiter_email') != recruiter_email:
        return jsonify({"error": "Unauthorized to update this application"}), 403
        
    # Extract fields
    job_title = data.get('job_title', '').strip()
    domain = data.get('domain', '').strip()
    job_type = data.get('job_type', '').strip()
    job_location = data.get('job_location', '').strip()
    openings = data.get('openings')
    deadline = data.get('deadline', '').strip()
    salary_min = data.get('salary_min')
    salary_max = data.get('salary_max')
    min_education = data.get('min_education', '').strip()
    min_experience = data.get('min_experience')
    required_skills_str = data.get('required_skills', '').strip()
    job_description = data.get('job_description', '').strip()
    interview_type = data.get('interview_type', '').strip()
    num_questions = data.get('num_questions')
    time_limit = data.get('time_limit')

    # Validation
    required_str_fields = [
        ("Job title", job_title),
        ("Domain", domain),
        ("Job type", job_type),
        ("Job location", job_location),
        ("Deadline", deadline),
        ("Minimum education", min_education),
        ("Required skills", required_skills_str),
        ("Job description", job_description),
        ("Interview type", interview_type)
    ]
    for field_name, value in required_str_fields:
        if not value:
            return jsonify({"error": f"{field_name} is required"}), 400

    # Type-cast numbers and validate
    try:
        openings = int(openings)
        if openings < 1:
            return jsonify({"error": "Number of openings must be at least 1"}), 400
    except (TypeError, ValueError):
        return jsonify({"error": "Openings must be a valid integer"}), 400

    try:
        salary_min = int(salary_min)
        salary_max = int(salary_max)
        if salary_min < 0 or salary_max < 0:
            return jsonify({"error": "Salary range values must be non-negative"}), 400
        if salary_min > salary_max:
            return jsonify({"error": "Minimum salary cannot be greater than maximum salary"}), 400
    except (TypeError, ValueError):
        return jsonify({"error": "Salary values must be valid integers"}), 400

    try:
        min_experience = int(min_experience)
        if min_experience < 0:
            return jsonify({"error": "Minimum experience cannot be negative"}), 400
    except (TypeError, ValueError):
        return jsonify({"error": "Minimum experience must be a valid integer"}), 400

    try:
        num_questions = int(num_questions)
        if not (1 <= num_questions <= 20):
            return jsonify({"error": "Number of questions must be between 1 and 20"}), 400
    except (TypeError, ValueError):
        return jsonify({"error": "Number of questions must be a valid integer"}), 400

    try:
        time_limit = int(time_limit)
        if not (5 <= time_limit <= 180):
            return jsonify({"error": "Time limit must be between 5 and 180 minutes"}), 400
    except (TypeError, ValueError):
        return jsonify({"error": "Time limit must be a valid integer"}), 400

    # Parse skills to array
    skills_list = [s.strip() for s in required_skills_str.split(',') if s.strip()]
    if not skills_list:
        return jsonify({"error": "At least one skill must be provided"}), 400

    # Update document in MongoDB
    update_data = {
        "job_title": job_title,
        "domain": domain,
        "job_type": job_type,
        "job_location": job_location,
        "openings": openings,
        "deadline": deadline,
        "salary_range": {
            "min": salary_min,
            "max": salary_max
        },
        "min_education": min_education,
        "min_experience": min_experience,
        "required_skills": skills_list,
        "job_description": job_description,
        "interview_type": interview_type,
        "num_questions": num_questions,
        "time_limit": time_limit,
        "updated_at": datetime.datetime.utcnow().isoformat()
    }

    try:
        applications_collection.update_one({"_id": app_obj_id}, {"$set": update_data})
        update_data['_id'] = app_id
        update_data['recruiter_email'] = recruiter_email
        return jsonify({"success": True, "application": update_data})
    except Exception as e:
        return jsonify({"error": f"Failed to update application: {str(e)}"}), 500


from bson.objectid import ObjectId

@app.route('/api/candidate/jobs/list', methods=['GET'])
def api_candidate_jobs_list():
    candidate_email = request.args.get('email', '').strip()
    try:
        jobs = list(applications_collection.find().sort("created_at", -1))
        enriched_jobs = []
        for job in jobs:
            job_id_str = str(job['_id'])
            job['_id'] = job_id_str
            
            # Fetch recruiter's latest company details
            recruiter = recruiter_collection.find_one({"email": job.get('recruiter_email')})
            if recruiter:
                job['company_name'] = recruiter.get('company_name', job.get('company_name', 'Acme Corp'))
                job['company_logo_path'] = recruiter.get('company_logo_path', job.get('company_logo_path', ''))
                job['company_industry'] = recruiter.get('company_industry', '')
                job['company_size'] = recruiter.get('company_size', '')
                job['company_location'] = recruiter.get('company_location', '')
                job['company_description'] = recruiter.get('company_description', '')
            else:
                job['company_industry'] = ''
                job['company_size'] = ''
                job['company_location'] = ''
                job['company_description'] = ''

            # Check if candidate already applied
            has_applied = False
            application_status = None
            applied_at = None
            if candidate_email:
                app_record = candidate_applications.find_one({
                    "job_id": job_id_str,
                    "candidate_email": candidate_email
                })
                if app_record:
                    has_applied = True
                    application_status = app_record.get('status', 'pending')
                    applied_at = app_record.get('applied_at')
            
            job['has_applied'] = has_applied
            job['application_status'] = application_status
            job['applied_at'] = applied_at
            enriched_jobs.append(job)
        return jsonify({"success": True, "jobs": enriched_jobs}), 200
    except Exception as e:
        return jsonify({"error": f"Failed to fetch jobs: {str(e)}"}), 500


@app.route('/api/candidate/jobs/match', methods=['POST'])
def api_candidate_jobs_match():
    data = request.get_json(silent=True) or {}
    job_id = data.get('job_id', '').strip()
    candidate_email = data.get('candidate_email', '').strip()
    
    if not job_id or not candidate_email:
        return jsonify({"error": "Job ID and Candidate Email are required"}), 400
        
    try:
        job = applications_collection.find_one({"_id": ObjectId(job_id)})
        if not job:
            return jsonify({"error": "Job opening not found"}), 404
            
        candidate = users_collection.find_one({"email": candidate_email})
        if not candidate:
            return jsonify({"error": "Candidate not found"}), 404
            
        # Parse resume and get CV Score
        cv_score = 0.0
        resume_skills_list = []
        doc = documents_collection.find_one({"user_email": candidate_email, "document_type": "resume"})
        
        if doc:
            relative_path = doc['file_path'].lstrip('/')
            abs_file_path = os.path.join(app.root_path, relative_path)
            try:
                with open(abs_file_path, 'rb') as f:
                    file_bytes = f.read()
                scan_result = scan_resume(file_bytes, doc['file_name'], job.get('job_description', ''))
                if scan_result.get('success'):
                    cv_score = scan_result.get('match_score', 0.0)
                    tech_scanned = scan_result.get('skills', {}).get('technical', [])
                    soft_scanned = scan_result.get('skills', {}).get('soft', [])
                    resume_skills_list = [s.lower().strip() for s in (tech_scanned + soft_scanned)]
            except Exception as e:
                print(f"Error scanning resume: {e}")
                
        # Parse profile skills
        profile_skills_str = candidate.get('skills', '')
        profile_skills = [s.lower().strip() for s in profile_skills_str.split(',') if s.strip()]
        combined_candidate_skills = set(profile_skills + resume_skills_list)
        
        # Skills match calculation
        required_skills = job.get('required_skills', [])
        matched_skills = []
        missing_skills = []
        for rs in required_skills:
            rs_clean = rs.lower().strip()
            if rs_clean in combined_candidate_skills or any(rs_clean in cs for cs in combined_candidate_skills):
                matched_skills.append(rs)
            else:
                missing_skills.append(rs)
                
        skills_match_pct = round((len(matched_skills) / len(required_skills)) * 100, 1) if required_skills else 100.0
        
        # Experience match calculation
        try:
            cand_exp = int(candidate.get('experience', 0) or 0)
        except ValueError:
            cand_exp = 0
        req_exp = int(job.get('min_experience', 0) or 0)
        exp_ok = cand_exp >= req_exp
        
        # Education match calculation
        cand_edu_str = candidate.get('education_str', '')
        cand_edu_entries = candidate.get('education', [])
        edu_names = [cand_edu_str.lower()]
        for entry in cand_edu_entries:
            if isinstance(entry, dict):
                edu_names.append(entry.get('qualification', '').lower())
                edu_names.append(entry.get('stream', '').lower())
                
        req_edu = job.get('min_education', '').lower().strip()
        edu_ok = False
        if not req_edu:
            edu_ok = True
        else:
            for name in edu_names:
                if req_edu in name or any(part in name for part in req_edu.split()):
                    edu_ok = True
                    break
                    
        # Formulate feedback message
        feedback_parts = []
        if required_skills:
            feedback_parts.append(f"You match {len(matched_skills)} out of {len(required_skills)} required skills ({', '.join(matched_skills[:4])}{'...' if len(matched_skills) > 4 else ''}; missing: {', '.join(missing_skills) if missing_skills else 'None'}).")
        else:
            feedback_parts.append("No specific required skills listed for this job.")
            
        if exp_ok:
            feedback_parts.append(f"Your {cand_exp} year(s) of experience meets the minimum requirement of {req_exp} year(s).")
        else:
            feedback_parts.append(f"Your {cand_exp} year(s) of experience is below the required {req_exp} year(s).")
            
        if edu_ok:
            feedback_parts.append(f"Your education background satisfies the '{job.get('min_education')}' requirement.")
        else:
            feedback_parts.append(f"Your education background may not fully satisfy the '{job.get('min_education')}' requirement.")
            
        match_message = " ".join(feedback_parts)
        
        return jsonify({
            "success": True,
            "cv_score": cv_score,
            "skills_match_pct": skills_match_pct,
            "match_message": match_message,
            "matched_skills": matched_skills,
            "missing_skills": missing_skills,
            "experience_ok": exp_ok,
            "education_ok": edu_ok
        }), 200
        
    except Exception as e:
        return jsonify({"error": f"Failed to compute match: {str(e)}"}), 500


@app.route('/api/candidate/apply', methods=['POST'])
def api_candidate_apply():
    data = request.get_json(silent=True) or {}
    job_id = data.get('job_id', '').strip()
    candidate_email = data.get('candidate_email', '').strip()
    
    if not job_id or not candidate_email:
        return jsonify({"error": "Job ID and Candidate Email are required"}), 400
        
    try:
        # Check if already applied
        existing = candidate_applications.find_one({"job_id": job_id, "candidate_email": candidate_email})
        if existing:
            return jsonify({"error": "You have already applied for this job opening."}), 400
            
        job = applications_collection.find_one({"_id": ObjectId(job_id)})
        if not job:
            return jsonify({"error": "Job opening not found"}), 404
            
        candidate = users_collection.find_one({"email": candidate_email})
        if not candidate:
            return jsonify({"error": "Candidate profile not found"}), 404
            
        # Parse resume and get CV Score
        cv_score = 0.0
        resume_skills_list = []
        doc = documents_collection.find_one({"user_email": candidate_email, "document_type": "resume"})
        
        if doc:
            relative_path = doc['file_path'].lstrip('/')
            abs_file_path = os.path.join(app.root_path, relative_path)
            try:
                with open(abs_file_path, 'rb') as f:
                    file_bytes = f.read()
                scan_result = scan_resume(file_bytes, doc['file_name'], job.get('job_description', ''))
                if scan_result.get('success'):
                    cv_score = scan_result.get('match_score', 0.0)
                    tech_scanned = scan_result.get('skills', {}).get('technical', [])
                    soft_scanned = scan_result.get('skills', {}).get('soft', [])
                    resume_skills_list = [s.lower().strip() for s in (tech_scanned + soft_scanned)]
            except Exception as e:
                print(f"Error scanning resume: {e}")
                
        # Parse profile skills
        profile_skills_str = candidate.get('skills', '')
        profile_skills = [s.lower().strip() for s in profile_skills_str.split(',') if s.strip()]
        combined_candidate_skills = set(profile_skills + resume_skills_list)
        
        # Skills match calculation
        required_skills = job.get('required_skills', [])
        matched_skills = []
        missing_skills = []
        for rs in required_skills:
            rs_clean = rs.lower().strip()
            if rs_clean in combined_candidate_skills or any(rs_clean in cs for cs in combined_candidate_skills):
                matched_skills.append(rs)
            else:
                missing_skills.append(rs)
                
        skills_match_pct = round((len(matched_skills) / len(required_skills)) * 100, 1) if required_skills else 100.0
        
        # Experience match calculation
        try:
            cand_exp = int(candidate.get('experience', 0) or 0)
        except ValueError:
            cand_exp = 0
        req_exp = int(job.get('min_experience', 0) or 0)
        exp_ok = cand_exp >= req_exp
        
        # Education match calculation
        cand_edu_str = candidate.get('education_str', '')
        cand_edu_entries = candidate.get('education', [])
        edu_names = [cand_edu_str.lower()]
        for entry in cand_edu_entries:
            if isinstance(entry, dict):
                edu_names.append(entry.get('qualification', '').lower())
                edu_names.append(entry.get('stream', '').lower())
                
        req_edu = job.get('min_education', '').lower().strip()
        edu_ok = False
        if not req_edu:
            edu_ok = True
        else:
            for name in edu_names:
                if req_edu in name or any(part in name for part in req_edu.split()):
                    edu_ok = True
                    break
                    
        # Formulate feedback message
        feedback_parts = []
        if required_skills:
            feedback_parts.append(f"You match {len(matched_skills)} out of {len(required_skills)} required skills ({', '.join(matched_skills[:4])}{'...' if len(matched_skills) > 4 else ''}; missing: {', '.join(missing_skills) if missing_skills else 'None'}).")
        else:
            feedback_parts.append("No specific required skills listed for this job.")
            
        if exp_ok:
            feedback_parts.append(f"Your {cand_exp} year(s) of experience meets the minimum requirement of {req_exp} year(s).")
        else:
            feedback_parts.append(f"Your {cand_exp} year(s) of experience is below the required {req_exp} year(s).")
            
        if edu_ok:
            feedback_parts.append(f"Your education background satisfies the '{job.get('min_education')}' requirement.")
        else:
            feedback_parts.append(f"Your education background may not fully satisfy the '{job.get('min_education')}' requirement.")
            
        match_message = " ".join(feedback_parts)
        
        # Fetch candidate documents
        docs_list = list(documents_collection.find({"user_email": candidate_email}))
        candidate_docs = []
        for d in docs_list:
            candidate_docs.append({
                "document_type": d.get('document_type'),
                "file_name": d.get('file_name'),
                "file_path": d.get('file_path'),
                "upload_timestamp": d.get('upload_timestamp')
            })
            
        new_app = {
            "job_id": job_id,
            "job_title": job.get('job_title'),
            "domain": job.get('domain'),
            "recruiter_email": job.get('recruiter_email'),
            "candidate_email": candidate_email,
            "candidate_name": candidate.get('full_name'),
            "candidate_phone": candidate.get('mobile'),
            "candidate_gender": candidate.get('gender'),
            "candidate_dob": candidate.get('dob'),
            "candidate_location": candidate.get('current_address'),
            "candidate_permanent_address": candidate.get('permanent_address'),
            "candidate_bio": candidate.get('bio'),
            "candidate_linkedin": candidate.get('linkedin'),
            "candidate_skills": candidate.get('skills'),
            "candidate_experience": cand_exp,
            "candidate_education_str": candidate.get('education_str'),
            "candidate_education_list": candidate.get('education', []),
            "candidate_documents": candidate_docs,
            "cv_score": cv_score,
            "skills_match_pct": skills_match_pct,
            "match_message": match_message,
            "status": "pending",
            "applied_at": datetime.datetime.utcnow().isoformat()
        }
        res = candidate_applications.insert_one(new_app)
        new_app['_id'] = str(res.inserted_id)
        
        return jsonify({"success": True, "application": new_app}), 201
        
    except Exception as e:
        return jsonify({"error": f"Application submission failed: {str(e)}"}), 500


@app.route('/api/recruiter/incoming-applications', methods=['GET'])
def api_recruiter_incoming_applications():
    email = request.args.get('email', '').strip()
    if not email:
        return jsonify({"error": "Recruiter email is required"}), 400
        
    try:
        apps = list(candidate_applications.find({"recruiter_email": email}).sort("applied_at", -1))
        for a in apps:
            a['_id'] = str(a['_id'])
            test_sub = a.get('test_submission')
            if test_sub and test_sub.get('test_id'):
                try:
                    from bson import ObjectId
                    test_doc = db['tests'].find_one({"_id": ObjectId(test_sub['test_id'])})
                    if test_doc:
                        test_sub['questions'] = test_doc.get('questions', [])
                except Exception as ex:
                    print(f"Error fetching test details for application: {ex}")
        return jsonify({"success": True, "applications": apps}), 200
    except Exception as e:
        return jsonify({"error": f"Failed to fetch incoming applications: {str(e)}"}), 500


@app.route('/api/recruiter/applications/action', methods=['POST'])
def api_recruiter_applications_action():
    data = request.get_json(silent=True) or {}
    application_id = data.get('application_id', '').strip()
    action = data.get('action', '').strip().lower() # 'accept' or 'discard'
    
    if not application_id or action not in ('accept', 'discard'):
        return jsonify({"error": "Valid application_id and action ('accept' or 'discard') are required"}), 400
        
    try:
        app_record = candidate_applications.find_one({"_id": ObjectId(application_id)})
        if not app_record:
            return jsonify({"error": "Application not found"}), 404
            
        status_value = 'accepted' if action == 'accept' else 'discarded'
        candidate_applications.update_one(
            {"_id": ObjectId(application_id)},
            {"$set": {"status": status_value, "decided_at": datetime.datetime.utcnow().isoformat()}}
        )
        
        # Trigger email notification in a safe try-except block so it doesn't fail the API call if credentials aren't set
        try:
            cand_email = app_record.get('candidate_email')
            cand_name = app_record.get('candidate_name', 'Candidate')
            job_title = app_record.get('job_title', 'Job Opening')
            
            # Fetch company name from recruiter profile
            recruiter = recruiter_collection.find_one({"email": app_record.get('recruiter_email')})
            company_name = recruiter.get('company_name', 'our company') if recruiter else 'our company'
            
            email_res = send_status_email(
                to_email=cand_email,
                user_name=cand_name,
                job_title=job_title,
                status=status_value,
                company_name=company_name
            )
            print(f"[EMAIL STATUS] Status email result for {cand_email}: {email_res}")
        except Exception as email_err:
            print(f"[EMAIL ERROR] Failed to send status email: {email_err}")
            
        return jsonify({"success": True, "message": f"Application successfully {status_value}"}), 200
        
    except Exception as e:
        return jsonify({"error": f"Failed to process application action: {str(e)}"}), 500


@app.route('/api/recruiter/parse-test', methods=['POST'])
def api_recruiter_parse_test():
    if 'file' not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
        
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No selected file"}), 400
        
    try:
        file_bytes = file.read()
        text = extract_text_from_file(file_bytes, file.filename)
        if not text.strip():
            return jsonify({"error": "The uploaded document is empty or could not be read."}), 400
            
        questions = parse_questions(text)
        if not questions:
            return jsonify({"error": "No multiple choice questions could be extracted from the document. Please check the format."}), 400
            
        return jsonify({
            "success": True,
            "questions": questions
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/recruiter/save-test', methods=['POST'])
def api_recruiter_save_test():
    try:
        data = request.get_json(silent=True) or {}
        recruiter_email = data.get('recruiter_email', '').strip()
        test_title = data.get('test_title', '').strip()
        questions = data.get('questions', [])
        test_id = data.get('test_id', '').strip()
        start_time = data.get('start_time', '').strip()
        end_time = data.get('end_time', '').strip()
        
        if not recruiter_email:
            return jsonify({"error": "Recruiter email is required"}), 400
        if not test_title:
            return jsonify({"error": "Test title is required"}), 400
        if not questions or not isinstance(questions, list):
            return jsonify({"error": "Questions list is required"}), 400
            
        for q in questions:
            if not q.get('question') or not q.get('options') or not q.get('answer'):
                return jsonify({"error": "Each question must have question text, options, and a correct answer"}), 400
                
        if start_time and end_time:
            if start_time >= end_time:
                return jsonify({"error": "End time must be after start time"}), 400
                
        from bson import ObjectId
        
        if test_id:
            # Update existing test
            existing_test = tests_collection.find_one({"_id": ObjectId(test_id), "recruiter_email": recruiter_email})
            if not existing_test:
                return jsonify({"error": "Test not found or unauthorized"}), 404
                
            tests_collection.update_one(
                {"_id": ObjectId(test_id)},
                {"$set": {
                    "test_title": test_title,
                    "questions": questions,
                    "start_time": start_time if start_time else None,
                    "end_time": end_time if end_time else None,
                    "updated_at": datetime.datetime.utcnow().isoformat()
                }}
            )
            return jsonify({
                "success": True,
                "message": "Test updated successfully!"
            }), 200
        else:
            # Create new test
            test_doc = {
                "recruiter_email": recruiter_email,
                "test_title": test_title,
                "questions": questions,
                "start_time": start_time if start_time else None,
                "end_time": end_time if end_time else None,
                "created_at": datetime.datetime.utcnow().isoformat()
            }
            tests_collection.insert_one(test_doc)
            return jsonify({
                "success": True,
                "message": "Test posted and saved successfully!"
            }), 201
    except Exception as e:
        return jsonify({"error": f"Failed to save test: {str(e)}"}), 500


@app.route('/api/recruiter/tests', methods=['GET'])
def api_recruiter_get_tests():
    try:
        recruiter_email = request.args.get('email', '').strip()
        if not recruiter_email:
            return jsonify({"error": "Recruiter email is required"}), 400
            
        tests = list(tests_collection.find({"recruiter_email": recruiter_email}))
        # Convert ObjectId to string for JSON serialization
        for t in tests:
            t['_id'] = str(t['_id'])
            
        return jsonify({
            "success": True,
            "tests": tests
        }), 200
    except Exception as e:
        return jsonify({"error": f"Failed to retrieve tests: {str(e)}"}), 500


@app.route('/api/recruiter/tests/delete', methods=['POST'])
def api_recruiter_delete_test():
    try:
        data = request.get_json(silent=True) or {}
        test_id = data.get('test_id', '').strip()
        recruiter_email = data.get('recruiter_email', '').strip()
        
        if not test_id:
            return jsonify({"error": "Test ID is required"}), 400
        if not recruiter_email:
            return jsonify({"error": "Recruiter email is required"}), 400
            
        from bson import ObjectId
        test = tests_collection.find_one({"_id": ObjectId(test_id), "recruiter_email": recruiter_email})
        if not test:
            return jsonify({"error": "Test not found or unauthorized"}), 404
            
        tests_collection.delete_one({"_id": ObjectId(test_id)})
        return jsonify({
            "success": True,
            "message": "Test deleted successfully"
        }), 200
    except Exception as e:
        return jsonify({"error": f"Failed to delete test: {str(e)}"}), 500


@app.route('/api/candidate/tests', methods=['GET'])
def api_candidate_get_tests():
    try:
        candidate_email = request.args.get('email', '').strip()
        if not candidate_email:
            return jsonify({"error": "Candidate email is required"}), 400
            
        # Find recruiters/jobs candidate applied to
        apps = list(candidate_applications.find({"candidate_email": candidate_email}))
        recruiter_emails = list(set([a.get('recruiter_email') for a in apps if a.get('recruiter_email')]))
        
        # Get tests from these recruiters
        tests = list(tests_collection.find({"recruiter_email": {"$in": recruiter_emails}}))
        
        candidate_tests = []
        for t in tests:
            test_id_str = str(t['_id'])
            
            # Check if candidate submitted this test for any of their applications with this recruiter
            submission_data = None
            for app in apps:
                if app.get('recruiter_email') == t.get('recruiter_email'):
                    test_sub = app.get('test_submission')
                    if test_sub and test_sub.get('test_id') == test_id_str:
                        submission_data = test_sub
                        break
            
            # Get recruiter company name
            recruiter = recruiter_collection.find_one({"email": t.get('recruiter_email')})
            company_name = recruiter.get('company_name', 'Acme Corp') if recruiter else 'Acme Corp'
            
            candidate_tests.append({
                "test_id": test_id_str,
                "test_title": t.get('test_title'),
                "recruiter_email": t.get('recruiter_email'),
                "company_name": company_name,
                "start_time": t.get('start_time'),
                "end_time": t.get('end_time'),
                "questions_count": len(t.get('questions', [])),
                "submitted": submission_data is not None,
                "submission": submission_data,
                "questions": t.get('questions', [])
            })
            
        return jsonify({
            "success": True,
            "tests": candidate_tests
        }), 200
    except Exception as e:
        return jsonify({"error": f"Failed to retrieve candidate tests: {str(e)}"}), 500


@app.route('/api/candidate/submit-test', methods=['POST'])
def api_candidate_submit_test():
    try:
        data = request.get_json(silent=True) or {}
        candidate_email = data.get('candidate_email', '').strip()
        test_id = data.get('test_id', '').strip()
        answers = data.get('answers', {})
        score = data.get('score', 0)
        strikes = data.get('strikes', 0)
        violations = data.get('violations', [])
        is_terminated = data.get('is_terminated', False) or (int(strikes) >= 3)
        
        if not candidate_email or not test_id:
            return jsonify({"error": "Candidate email and Test ID are required"}), 400
            
        from bson import ObjectId
        test = tests_collection.find_one({"_id": ObjectId(test_id)})
        if not test:
            return jsonify({"error": "Test not found"}), 404
            
        recruiter_email = test.get('recruiter_email')
        
        # Find matching application
        app_record = candidate_applications.find_one({
            "candidate_email": candidate_email,
            "recruiter_email": recruiter_email
        }, sort=[("applied_at", -1)])
        
        if not app_record:
            return jsonify({"error": "No application found for this recruiter's job openings."}), 404
            
        test_submission = {
            "test_id": test_id,
            "test_title": test.get('test_title'),
            "answers": answers,
            "score": score,
            "strikes": strikes,
            "violations": violations,
            "is_terminated": bool(is_terminated),
            "submitted_at": datetime.datetime.utcnow().isoformat(),
            "oa_status": "pending"
        }
        
        candidate_applications.update_one(
            {"_id": app_record["_id"]},
            {"$set": {"test_submission": test_submission}}
        )
        
        return jsonify({
            "success": True,
            "message": "Test submitted successfully!"
        }), 200
    except Exception as e:
        return jsonify({"error": f"Failed to submit test: {str(e)}"}), 500


@app.route('/api/recruiter/applications/oa-action', methods=['POST'])
def api_recruiter_applications_oa_action():
    try:
        data = request.get_json(silent=True) or {}
        application_id = data.get('application_id', '').strip()
        action = data.get('action', '').strip().lower() # 'pass' or 'fail'
        reason = data.get('reason', '').strip()
        
        if not application_id or action not in ('pass', 'fail'):
            return jsonify({"error": "Valid application_id and action ('pass' or 'fail') are required"}), 400
            
        if action == 'fail' and not reason:
            return jsonify({"error": "A reason is required to fail the Online Assessment"}), 400
            
        from bson import ObjectId
        app_record = candidate_applications.find_one({"_id": ObjectId(application_id)})
        if not app_record:
            return jsonify({"error": "Application not found"}), 404
            
        test_sub = app_record.get('test_submission')
        if not test_sub:
            return jsonify({"error": "Candidate has not submitted the Online Assessment yet"}), 400
            
        status_value = 'passed' if action == 'pass' else 'failed'
        
        update_fields = {
            "test_submission.oa_status": status_value,
            "test_submission.decided_at": datetime.datetime.utcnow().isoformat()
        }
        if action == 'fail':
            update_fields["test_submission.fail_reason"] = reason
            
        candidate_applications.update_one(
            {"_id": ObjectId(application_id)},
            {"$set": update_fields}
        )
        
        # Trigger email notification
        if action == 'pass':
            try:
                from modules.email_service import send_oa_pass_email
                cand_email = app_record.get('candidate_email')
                cand_name = app_record.get('candidate_name', 'Candidate')
                test_title = test_sub.get('test_title', 'Online Assessment')
                score = test_sub.get('score', 0)
                
                # Fetch company name
                recruiter = recruiter_collection.find_one({"email": app_record.get('recruiter_email')})
                company_name = recruiter.get('company_name', 'our company') if recruiter else 'our company'
                
                email_res = send_oa_pass_email(
                    to_email=cand_email,
                    user_name=cand_name,
                    test_title=test_title,
                    score=score,
                    company_name=company_name
                )
                print(f"[EMAIL OA] Congratulatory email result for {cand_email}: {email_res}")
            except Exception as email_err:
                print(f"[EMAIL OA ERROR] Failed to send congratulations email: {email_err}")
                
        return jsonify({"success": True, "message": f"Online Assessment status successfully updated to {status_value}."}), 200
        
    except Exception as e:
        return jsonify({"error": f"Failed to process OA action: {str(e)}"}), 500


if __name__ == '__main__':
    print("\n" + "=" * 60)
    print("  DecentraHire - AI-Powered Recruitment Platform")
    print("  MockVue Interview Practice Enabled")
    print("  Starting on http://localhost:5000")
    print("=" * 60 + "\n")
    app.run(debug=True, port=5000)
