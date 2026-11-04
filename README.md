# 🌐 DecentraHire — AI-Proctored Candidate Assessment Platform

DecentraHire is a modern, premium recruitment and candidate screening ecosystem. It features an advanced real-time AI eye-gaze and head-rotation proctoring system, automated exam workflows, smart verification engines (resume parsing, certificate OCR, DigiLocker integration), and comprehensive recruiter analysis dashboards.

---

## 📁 Repository Directory Structure

To keep the repository highly maintainable, organized, and scalable, files are arranged in a modular, clean folder layout:

```text
decentrahire/
│
├── ⚙️ app.py                   # Main Flask application (Routing, Controllers, APIs)
├── 📝 requirements.txt          # Python external libraries and dependencies
├── 🔒 .env                      # Application environment variables & secret keys (ignored by git)
├── 🛡️ .gitignore                # Lists files/directories ignored by Git version control
│
├── 📂 modules/                 # Core domain service engines and business logic
│   ├── __init__.py
│   ├── audio_transcriber.py    # Transcribes audio files for spoken interview sessions
│   ├── certificate_scanner.py  # Performs OCR on uploaded certificates
│   ├── digilocker_verifier.py  # Mocks verification integration with DigiLocker API
│   ├── email_service.py        # Connects with Gmail SMTP to mail passed candidates
│   ├── interview_practice.py   # Manages practice interview simulations
│   ├── resume_scanner.py       # Reads and parses candidate resumes
│   ├── test_parser.py          # Parses scheduled quizzes and assessment questions
│   └── verification_engine.py  # Central orchestrator for all verification microservices
│
├── 📂 templates/               # Frontend HTML5 semantic templates
│   └── index.html              # Unified glassmorphic candidate/recruiter SPA interface
│
├── 📂 static/                  # Static design and behavior assets
│   ├── 🎨 css/
│   │   ├── style.css           # Core premium styles and glassmorphism styling
│   │   └── mockvue.css         # Utility styling for mock interfaces
│   ├── ⚡ js/
│   │   ├── app.js              # Real-time MediaPipe gaze-tracker and dashboard controller
│   │   └── mockvue.js          # Client-side utility routines
│   └── 📂 uploads/             # Stores candidate uploaded resumes/certificates (local)
│
├── 📂 tests/                   # Automated integration verification suite
│   ├── test_integration.py     # Exam taking, score counting, and DB pipeline flow test
│   ├── test_malpractice_rendering.py # Auto-lockout and recruiter dashboard warning test
│   └── test_oa_failure.py      # Custom recruiter feedback message persistence test
│
├── 📂 scripts/                 # Development and administrative utilities
│   ├── check_db.py             # Database connectivity and record inspector
│   ├── check_routes.py         # Diagnostic endpoint tester
│   ├── make_test_live.py       # Developer tool to set schedules active for manual testing
│   ├── reset_db_passwords.py   # Resets developer test user credentials to 'Password123'
│   ├── submit_test_for_ui.py   # Mock submits high-score results for candidate dashboard checks
│   └── submit_malpractice_for_ui.py # Mock submits proctoring malpractice for recruiter visual reviews
│
└── 📂 data/                    # JSON mock databases and static files
    └── digilocker_mock_db.json # Base mock data repository for credential verification
```

---

## 🚀 Getting Started

### 1. Prerequisites
Ensure you have **Python 3.9+** and **MongoDB** installed and running.

### 2. Installation
Install all package dependencies via `pip`:
```bash
pip install -r requirements.txt
```

### 3. Environment Setup
Configure your environment variables inside the `.env` file at the root directory:
```env
MONGO_URI=mongodb+srv://... (or local mongodb://localhost:27017/decentrahire)
GEMINI_API_KEY=your-gemini-key
SENDER_EMAIL=your-email@gmail.com
SENDER_PASSWORD=your-app-password
```

### 4. Running the Application
Start the Flask development server:
```bash
python app.py
```
The server will boot on `http://localhost:5000`.

---

## 🛡️ Proctoring Features

DecentraHire's examination lobby runs a low-overhead real-time webcam analyzer utilizing **MediaPipe Face Mesh** with a custom mathematical estimator:
1. **Calibration**: The candidate's first 3 seconds (20 frames) establish baseline horizontal/vertical gaze ratios, head turn (yaw), and head tilt (pitch).
2. **Real-time Checks**:
   - **Gaze Deviation**: Iris positioning is parsed to ensure focus stays on screen.
   - **Head Rotation (Yaw/Pitch)**: Tracks physical turn deviations left, right, up, or down.
   - **Environment Integrity**: Flags if the face is hidden or if multiple faces appear in the camera view.
   - **Window Focus**: Tracks browser tab switches and exits from fullscreen mode.
3. **Violations Policy**: Any active deviation persisting for `>= 0.75 seconds` (3 consecutive frames) logs a proctoring violation. If a candidate receives **3 strikes**, the assessment terminates immediately and submits malpractice reports to the recruiter.

---

## 🧪 Testing Suite & Administration

### Running Tests
To run the automated tests, ensure the Flask app is running locally, then execute:
```bash
# Run candidate assessment flow test
python tests/test_integration.py

# Run proctoring malpractice test
python tests/test_malpractice_rendering.py

# Run recruiter failure reason flow test
python tests/test_oa_failure.py
```

### Utility Scripts
Manage the application database or schedule assessments easily:
```bash
# Verify MongoDB collections and documents
python scripts/check_db.py

# Reset developer account credentials to 'Password123'
python scripts/reset_db_passwords.py

# Activate a scheduled test immediately for manual UI walkthrough
python scripts/make_test_live.py
```

---

## 💡 Developer Guidelines
* **Keeping it Modular**: When writing new business rules, add files inside the `modules/` directory. Keep `app.py` primarily focused on request routing.
* **Static Assets**: Put styles inside `static/css/style.css` and JavaScript inside `static/js/app.js` to ensure the core glassmorphic theme is uniform.

  Team Members :
  Nikeeta Pande
  Sanjana Nikam 
  Kunal Paraskar 
  Nehal Pande
  Shridhar Panigrahi
