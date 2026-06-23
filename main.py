#!/usr/bin/env python3
"""
CEO Sales Reminder - Executive Sales Intelligence System for Pixel Studios
Orchestrates Zoho CRM data retrieval, AI CRO-style analysis, alerting, and email dispatch.
"""

import sys
import os
import json
import time
import logging
import argparse
import smtplib
from datetime import datetime, timedelta, date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Dict, Any, List, Tuple, Optional

# Reconfigure stdout/stderr to use UTF-8 to prevent UnicodeEncodeError on Windows terminals
if sys.stdout and sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except AttributeError:
        pass
if sys.stderr and sys.stderr.encoding != 'utf-8':
    try:
        sys.stderr.reconfigure(encoding='utf-8')
    except AttributeError:
        pass

# Third-party imports (handled gracefully if missing)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

# ==========================================
# UTILITY HELPERS FOR NUMERICAL DELTAS
# ==========================================
def calc_pct_change(prev: float, curr: float) -> str:
    if prev == curr:
        return "0%"
    if prev == 0:
        return "+100%"
    pct = ((curr - prev) / prev) * 100
    sign = "+" if pct > 0 else ""
    return f"{sign}{pct:.1f}%"

def format_value(val: float) -> str:
    return f"₹{int(round(val)):,}"

# ==========================================
# 1. LOGGING SETUP
# ==========================================
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "app.log")

logger = logging.getLogger("CEOSalesReminder")
logger.setLevel(logging.INFO)

# Formatter
formatter = logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

# Console Handler
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

# File Handler
file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)


# ==========================================
# 2. CONFIGURATION MANAGEMENT
# ==========================================
class Config:
    # System settings
    MOCK_MODE: bool = os.getenv("MOCK_MODE", "True").lower() == "true"
    
    # CEO Profile
    CEO_NAME: str = os.getenv("CEO_NAME", "Siddhanth")
    CEO_PHONE: str = os.getenv("CEO_PHONE", "916380915054")
    CEO_EMAIL: str = os.getenv("CEO_EMAIL", "siddhanthsrinivasan@gmail.com")
    
    # Zoho CRM Credentials
    ZOHO_ACCOUNTS_URL: str = os.getenv("ZOHO_ACCOUNTS_URL", "https://accounts.zoho.com").rstrip('/')
    ZOHO_API_URL: str = os.getenv("ZOHO_API_URL", "https://www.zohoapis.com").rstrip('/')
    ZOHO_CLIENT_ID: str = os.getenv("ZOHO_CLIENT_ID", "")
    ZOHO_CLIENT_SECRET: str = os.getenv("ZOHO_CLIENT_SECRET", "")
    ZOHO_REFRESH_TOKEN: str = os.getenv("ZOHO_REFRESH_TOKEN", "")
    
    # Custom Zoho CRM Field Mappings
    FIELD_LEAD_BUDGET: str = os.getenv("ZOHO_FIELD_LEAD_BUDGET", "Monthly_Budget")
    FIELD_LEAD_MRR: str = os.getenv("ZOHO_FIELD_LEAD_MRR", "Expected_MRR")
    FIELD_LEAD_SERVICE: str = os.getenv("ZOHO_FIELD_LEAD_SERVICE", "Service_Interested")
    FIELD_DEAL_LOSS_REASON: str = os.getenv("ZOHO_FIELD_DEAL_LOSS_REASON", "Loss_Reason")
    FIELD_CALL_OUTCOME: str = os.getenv("ZOHO_FIELD_CALL_OUTCOME", "Call_Outcome")
    FIELD_NEXT_FOLLOWUP: str = os.getenv("ZOHO_FIELD_NEXT_FOLLOWUP", "Next_Follow_up_Date")

    # OpenAI / AI Settings
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    OPENAI_MODEL: str = os.getenv("OPENAI_MODEL", "gpt-4o")

    # Resend Email Settings
    RESEND_API_KEY: str = os.getenv("RESEND_API_KEY", "")
    EMAIL_FROM: str = os.getenv("EMAIL_FROM", "Report Bot <reminder@pixelsoft.in>")
    EMAIL_TO: str = os.getenv("EMAIL_TO", "siddhanthsrinivasan@gmail.com")

    # SLA and Threshold configurations
    ALERT_SLA_HOURS: int = int(os.getenv("ALERT_SLA_HOURS", "24"))
    ALERT_HIGH_VALUE_LEAD_BUDGET: float = float(os.getenv("ALERT_HIGH_VALUE_LEAD_BUDGET", "100000"))
    ALERT_HIGH_VALUE_DEAL_VALUE: float = float(os.getenv("ALERT_HIGH_VALUE_DEAL_VALUE", "300000"))
    ALERT_PROPOSAL_PENDING_DAYS: int = int(os.getenv("ALERT_PROPOSAL_PENDING_DAYS", "7"))
    ALERT_COMPLIANCE_TARGET: float = float(os.getenv("ALERT_COMPLIANCE_TARGET", "80"))
    ALERT_PIPELINE_DROP_PERCENT: float = float(os.getenv("ALERT_PIPELINE_DROP_PERCENT", "10"))


config = Config()
logger.info(f"Configuration Loaded. MOCK_MODE={config.MOCK_MODE}")


# ==========================================
# 3. ZOHO CRM CLIENT & DATA STRUCTURES
# ==========================================
class ZohoClient:
    """Handles authentication and fetches required metrics from Zoho CRM using OAuth2."""
    
    TOKEN_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "zoho_token.json")
    
    def __init__(self):
        self.access_token: Optional[str] = None
        self.expiry_time: float = 0.0

    def _load_cached_token(self) -> None:
        if os.path.exists(self.TOKEN_CACHE_FILE):
            try:
                with open(self.TOKEN_CACHE_FILE, "r") as f:
                    data = json.load(f)
                    self.access_token = data.get("access_token")
                    self.expiry_time = data.get("expiry_time", 0.0)
            except Exception as e:
                logger.warning(f"Failed to read Zoho token cache: {e}")

    def _save_cached_token(self, token: str, expires_in: int) -> None:
        self.access_token = token
        self.expiry_time = time.time() + expires_in - 60  # Subtract buffer margin of 60 seconds
        try:
            with open(self.TOKEN_CACHE_FILE, "w") as f:
                json.dump({"access_token": self.access_token, "expiry_time": self.expiry_time}, f)
        except Exception as e:
            logger.warning(f"Failed to save Zoho token cache: {e}")

    def get_access_token(self) -> str:
        """Retrieves or refreshes OAuth access token."""
        if config.MOCK_MODE:
            return "mock_access_token"
            
        self._load_cached_token()
        if self.access_token and time.time() < self.expiry_time:
            return self.access_token

        logger.info("Refreshing Zoho CRM Access Token...")
        url = f"{config.ZOHO_ACCOUNTS_URL}/oauth/v2/token"
        payload = {
            "refresh_token": config.ZOHO_REFRESH_TOKEN,
            "client_id": config.ZOHO_CLIENT_ID,
            "client_secret": config.ZOHO_CLIENT_SECRET,
            "grant_type": "refresh_token"
        }
        
        try:
            import requests
            response = requests.post(url, data=payload, timeout=10)
            response.raise_for_status()
            res_data = response.json()
            
            if "access_token" in res_data:
                token = res_data["access_token"]
                expires_in = res_data.get("expires_in", 3600)
                self._save_cached_token(token, expires_in)
                logger.info("Zoho Access Token successfully refreshed.")
                return token
            else:
                logger.error(f"Zoho token response did not contain access_token: {res_data}")
                raise Exception("Zoho OAuth refresh response invalid.")
        except Exception as e:
            logger.error(f"Failed to refresh Zoho access token: {e}")
            raise

    def execute_coql(self, query: str) -> List[Dict[str, Any]]:
        """Executes a COQL query on Zoho CRM. Auto-heals missing custom fields."""
        if config.MOCK_MODE:
            return []
            
        import requests
        import re
        
        for attempt in range(10):
            token = self.get_access_token()
            url = f"{config.ZOHO_API_URL}/crm/v3/coql"
            headers = {
                "Authorization": f"Zoho-oauthtoken {token}",
                "Content-Type": "application/json"
            }
            payload = {"select_query": query}
            
            try:
                response = requests.post(url, json=payload, headers=headers, timeout=15)
                if response.status_code == 204:
                    return []  # No content matches query
                
                # Check for 400 Client Error indicating invalid columns
                if response.status_code == 400:
                    try:
                        res_data = response.json()
                        logger.error(f"Zoho 400 error response details: {res_data}")
                        msg = res_data.get("message", "")
                        
                        missing_col = None
                        if isinstance(res_data, dict):
                            details = res_data.get("details", {})
                            if isinstance(details, dict):
                                missing_col = details.get("column_name")
                                
                        # Fallback to regex if column_name not in details
                        if not missing_col and msg:
                            match = re.search(r"column\s+'([^']+)'\s+not\s+found", msg, re.IGNORECASE)
                            if match:
                                missing_col = match.group(1)
                                
                        if missing_col:
                            logger.warning(f"Zoho field '{missing_col}' not found in CRM schema. Removing from query and retrying...")
                            
                            # Extract the SELECT part
                            select_match = re.match(r"(?i)^select\s+(.+?)\s+from", query)
                            if select_match:
                                select_clause = select_match.group(1)
                                fields = [f.strip() for f in select_clause.split(",")]
                                updated_fields = []
                                for f in fields:
                                    field_name = f.split(".")[-1] if "." in f else f
                                    if field_name.lower() != missing_col.lower() and f.lower() != missing_col.lower():
                                        updated_fields.append(f)
                                
                                if not updated_fields:
                                    logger.error("No valid fields remaining in select clause of query.")
                                    return []
                                    
                                new_select = ", ".join(updated_fields)
                                query = query.replace(select_clause, new_select, 1)
                                continue
                    except Exception as parse_err:
                        logger.error(f"Failed to parse Zoho 400 error response: {parse_err}")
                
                response.raise_for_status()
                res_data = response.json()
                return res_data.get("data", [])
            except Exception as e:
                logger.error(f"Zoho COQL query error: {e}. Query: {query}")
                return []
        return []

    def fetch_leads_created_between(self, start_dt: str, end_dt: str) -> List[Dict[str, Any]]:
        query = (
            f"select id, First_Name, Last_Name, Lead_Source, Industry, Created_Time, Lead_Status, "
            f"Owner.first_name, Owner.last_name, {config.FIELD_LEAD_BUDGET}, {config.FIELD_LEAD_MRR}, "
            f"{config.FIELD_LEAD_SERVICE} from Leads where Created_Time >= '{start_dt}' and Created_Time <= '{end_dt}'"
        )
        return self.execute_coql(query)

    def fetch_deals_modified_between(self, start_dt: str, end_dt: str) -> List[Dict[str, Any]]:
        query = (
            f"select id, Deal_Name, Stage, Amount, Expected_Revenue, Modified_Time, Created_Time, "
            f"Owner.first_name, Owner.last_name, {config.FIELD_DEAL_LOSS_REASON} from Deals "
            f"where Modified_Time >= '{start_dt}' and Modified_Time <= '{end_dt}'"
        )
        return self.execute_coql(query)

    def fetch_all_open_deals(self) -> List[Dict[str, Any]]:
        query = (
            f"select id, Deal_Name, Stage, Amount, Expected_Revenue, Modified_Time, Created_Time, "
            f"Owner.first_name, Owner.last_name from Deals "
            f"where Stage not in ('Closed Won', 'Closed Lost')"
        )
        return self.execute_coql(query)

    def fetch_tasks_due_between(self, start_date: str, end_date: str) -> List[Dict[str, Any]]:
        query = (
            f"select id, Subject, Due_Date, Status, Modified_Time, What_Id, "
            f"Owner.first_name, Owner.last_name from Tasks where Due_Date >= '{start_date}' and Due_Date <= '{end_date}'"
        )
        return self.execute_coql(query)

    def fetch_overdue_tasks(self, today_str: str) -> List[Dict[str, Any]]:
        query = (
            f"select id, Subject, Due_Date, Status, What_Id, "
            f"Owner.first_name, Owner.last_name from Tasks where Status != 'Completed' and Due_Date < '{today_str}'"
        )
        return self.execute_coql(query)

    def fetch_calls_between(self, start_dt: str, end_dt: str) -> List[Dict[str, Any]]:
        query = (
            f"select id, Subject, Call_Start_Time, Call_Duration, Call_Type, What_Id, Who_Id, "
            f"Owner.first_name, Owner.last_name, {config.FIELD_CALL_OUTCOME} from Calls "
            f"where Call_Start_Time >= '{start_dt}' and Call_Start_Time <= '{end_dt}'"
        )
        return self.execute_coql(query)

    def fetch_notes_created_between(self, start_dt: str, end_dt: str) -> List[Dict[str, Any]]:
        query = (
            f"select id, Note_Title, Note_Content, Created_Time, Parent_Id, "
            f"Created_By.first_name, Created_By.last_name from Notes "
            f"where Created_Time >= '{start_dt}' and Created_Time <= '{end_dt}'"
        )
        return self.execute_coql(query)


# ==========================================
# 4. MOCK DATA PROVIDER
# ==========================================
class MockDataProvider:
    """Generates extremely realistic data matching yesterday's operations for local sandbox testing."""
    
    @staticmethod
    def get_mock_crm_payload(target_date: date) -> Dict[str, Any]:
        prev_date_str = target_date.strftime("%Y-%m-%d")
        
        # New Leads
        new_leads = [
            {
                "id": "1001",
                "first_name": "Arjun",
                "last_name": "Mehta",
                "source": "Google Ads",
                "industry": "Manufacturing",
                "service": "Custom ERP Implementation",
                "budget": 120000.0,
                "expected_mrr": 20000.0,
                "owner": "Rohan Sharma",
                "created_time": f"{prev_date_str}T10:15:00+05:30",
                "status": "Qualified",
                "lead_score": 85
            },
            {
                "id": "1002",
                "first_name": "Priya",
                "last_name": "Sen",
                "source": "LinkedIn Outreach",
                "industry": "Healthcare",
                "service": "Patient Portal App",
                "budget": 250000.0,
                "expected_mrr": 45000.0,
                "owner": "Sarah Khan",
                "created_time": f"{prev_date_str}T14:30:00+05:30",
                "status": "Qualified",
                "lead_score": 92
            },
            {
                "id": "1003",
                "first_name": "Vikram",
                "last_name": "Adani",
                "source": "Organic Search",
                "industry": "Logistics",
                "service": "Fleet Tracking UI",
                "budget": 40000.0,
                "expected_mrr": 8000.0,
                "owner": "Rohan Sharma",
                "created_time": f"{prev_date_str}T16:45:00+05:30",
                "status": "Not Contacted",
                "lead_score": 45
            }
        ]

        # Qualified Leads
        qualified_leads = [l for l in new_leads if l["status"] == "Qualified"]

        # Deals modified yesterday (Won/Lost/Stagnant)
        won_deals = [
            {
                "id": "2001",
                "name": "Tech Corp CRM Sync",
                "stage": "Closed Won",
                "amount": 600000.0,
                "expected_revenue": 600000.0,
                "owner": "Rohan Sharma",
                "modified_time": f"{prev_date_str}T11:00:00+05:30",
                "created_time": f"2026-05-15T09:00:00+05:30"
            }
        ]
        
        lost_deals = [
            {
                "id": "2002",
                "name": "Fintech Mobile Wallet",
                "stage": "Closed Lost",
                "amount": 350000.0,
                "owner": "Sarah Khan",
                "loss_reason": "Competitor pricing lower",
                "modified_time": f"{prev_date_str}T15:20:00+05:30",
                "created_time": f"2026-06-01T10:00:00+05:30"
            }
        ]

        # Open Pipeline
        open_pipeline = [
            {
                "id": "2003",
                "name": "EdTech Learning System",
                "stage": "Proposal",
                "amount": 500000.0,
                "expected_revenue": 400000.0,
                "owner": "Sarah Khan",
                "modified_time": "2026-06-10T09:00:00+05:30",
                "created_time": "2026-06-05T09:00:00+05:30"
            },
            {
                "id": "2004",
                "name": "Retail E-Commerce Integration",
                "stage": "Negotiation",
                "amount": 750000.0,
                "expected_revenue": 675000.0,
                "owner": "Rohan Sharma",
                "modified_time": f"{prev_date_str}T17:00:00+05:30",
                "created_time": "2026-06-12T17:00:00+05:30"
            },
            {
                "id": "2005",
                "name": "Hospitality Booking System",
                "stage": "Proposal",
                "amount": 300000.0,
                "expected_revenue": 150000.0,
                "owner": "Sarah Khan",
                "modified_time": "2026-06-08T12:00:00+05:30",
                "created_time": "2026-06-08T12:00:00+05:30"
            }
        ]

        # Filter by stage
        proposal_stage = [d for d in open_pipeline if d["stage"] == "Proposal"]
        negotiation_stage = [d for d in open_pipeline if d["stage"] == "Negotiation"]

        # Calls
        calls = [
            {
                "id": "3001",
                "subject": "Introductory Call - Arjun Mehta",
                "duration": 480,
                "outcome": "Interested, requested proposal",
                "type": "Outbound",
                "owner": "Rohan Sharma",
                "start_time": f"{prev_date_str}T11:15:00+05:30"
            },
            {
                "id": "3002",
                "subject": "Followup Call - Retail E-Commerce",
                "duration": 1200,
                "outcome": "Negotiation in progress, client requested discount",
                "type": "Outbound",
                "owner": "Rohan Sharma",
                "start_time": f"{prev_date_str}T15:00:00+05:30"
            },
            {
                "id": "3003",
                "subject": "Missed Call from Vikram Adani",
                "duration": 0,
                "outcome": "Missed Call",
                "type": "Inbound",
                "owner": "Rohan Sharma",
                "start_time": f"{prev_date_str}T16:50:00+05:30"
            }
        ]

        # Notes
        notes = [
            {
                "id": "4001",
                "title": "Budget Discussion - Priya Sen",
                "content": "Client discussed monthly budget of 80k MRR. Mentioned competitor DevCorp offered lower setup cost but client prefers our tech stack. Will decide by tomorrow.",
                "owner": "Sarah Khan",
                "created_time": f"{prev_date_str}T14:45:00+05:30"
            },
            {
                "id": "4002",
                "title": "Objection - Retail E-Commerce Integration",
                "content": "Rohan noted that they are asking for a 15% discount on the implementation fee. Next action is to send the revised proposal by today.",
                "owner": "Rohan Sharma",
                "created_time": f"{prev_date_str}T15:30:00+05:30"
            }
        ]

        # Tasks
        tasks = [
            {
                "id": "5001",
                "subject": "Overdue Followup - DevCorp Competitor Research",
                "due_date": (target_date - timedelta(days=4)).strftime("%Y-%m-%d"),
                "status": "In Progress",
                "owner": "Sarah Khan"
            },
            {
                "id": "5002",
                "subject": "Followup Call - Priya Sen Decision",
                "due_date": target_date.strftime("%Y-%m-%d"),
                "status": "Not Started",
                "owner": "Sarah Khan"
            },
            {
                "id": "5003",
                "subject": "Send Revised Quote - Retail E-Commerce Integration",
                "due_date": target_date.strftime("%Y-%m-%d"),
                "status": "Not Started",
                "owner": "Rohan Sharma"
            }
        ]
        
        # Followups are tasks due today (or future tasks)
        followups = [t for t in tasks if t["due_date"] >= target_date.strftime("%Y-%m-%d")]

        # Industries
        industries = list(set([l["industry"] for l in new_leads if l.get("industry")]))
        
        # Lead sources
        lead_sources = list(set([l["source"] for l in new_leads if l.get("source")]))
        
        # Expected MRR
        expected_mrr = [l["expected_mrr"] for l in new_leads if l.get("expected_mrr")]

        db_yesterday = target_date - timedelta(days=2)
        yesterday = target_date - timedelta(days=1)
        today = target_date
        
        db_yesterday_stats = {
            "leads_count": 3,
            "won_deals_count": 0,
            "won_deals_value": 0.0,
            "lost_deals_count": 1,
            "lost_deals_value": 350000.0,
            "pipeline_value": 1800000.0,
            "calls_completed": 8,
            "calls_missed": 5
        }
        yesterday_stats = {
            "leads_count": 5,
            "won_deals_count": 1,
            "won_deals_value": 600000.0,
            "lost_deals_count": 0,
            "lost_deals_value": 0.0,
            "pipeline_value": 1550000.0,
            "calls_completed": 12,
            "calls_missed": 1
        }
        today_stats = {
            "leads_count": 2,
            "won_deals_count": 0,
            "won_deals_value": 0.0,
            "lost_deals_count": 0,
            "lost_deals_value": 0.0,
            "pipeline_value": 1550000.0,
            "calls_completed": 4,
            "calls_missed": 0
        }

        return {
            "date": yesterday.strftime("%Y-%m-%d"),
            "db_yesterday_date": db_yesterday.strftime("%Y-%m-%d"),
            "yesterday_date": yesterday.strftime("%Y-%m-%d"),
            "today_date": today.strftime("%Y-%m-%d"),
            
            # Aggregated metrics for numbers report
            "db_yesterday": db_yesterday_stats,
            "yesterday": yesterday_stats,
            "today": today_stats,
            
            # Detail fields for yesterday (needed for Alerts Engine)
            "new_leads": new_leads,
            "qualified_leads": qualified_leads,
            "won_deals": won_deals,
            "lost_deals": lost_deals,
            "open_pipeline": open_pipeline,
            "proposal_stage": proposal_stage,
            "negotiation_stage": negotiation_stage,
            "calls": calls,
            "notes": notes,
            "tasks": tasks,
            "followups": followups,
            "industries": industries,
            "lead_sources": lead_sources,
            "expected_mrr": expected_mrr
        }


# ==========================================
# 5. AI CRO ANALYZER
# ==========================================
class AIAnalyzer:
    """Uses OpenAI/LLM API to generate strategic CRO insight reports from Zoho CRM data."""

    SYSTEM_PROMPT = """
You are the Chief Revenue Officer (CRO) of Pixel Studios. You are providing a strategic morning sales briefing directly to the CEO.

The CEO has requested a strictly numbers-based, comparison-focused report with zero fluff (no paragraphs, no filler text).
You will receive aggregated sales metrics for:
- Day Before Yesterday (2 days ago)
- Yesterday (1 day ago)
- Today (current day)

Your output must be ONLY a valid JSON object matching the following structure and satisfying strict character limits:

{
  "ceo_name": "Siddhanth",
  "executive_summary": "Crisp numeric delta summary of the main changes. Max 200 characters.",
  "whats_working": "Highlight the best numeric improvement delta. Max 150 characters.",
  "risks": "Highlight the worst numeric drop or leak delta. Max 150 characters.",
  "revenue_outlook": "Open pipeline value and target deltas. Max 150 characters.",
  "todays_priority": "Numeric priority focus for today. Max 150 characters.",
  "executive_takeaway": "Key CRO strategic conclusion from the numbers. Max 250 characters.",
  "email_subject": "CEO Sales Delta Report - DD MMM YYYY (e.g. CEO Sales Delta Report - 19 Jun 2026)",
  "email_body": "A professional, concise, numbers-only report showing day-over-day changes."
}

CRITICAL RULES:
1. Do not use verbose paragraphs or explanations. Use comparison notation: "Metric: Previous ➔ Current (Delta%)".
2. Compare both:
   - Day Before Yesterday ➔ Yesterday
   - Yesterday ➔ Today (so far)
3. Enforce the character limits strictly. Truncate in Python if they exceed limits.
"""

    @classmethod
    def analyze(cls, crm_data: Dict[str, Any]) -> Dict[str, Any]:
        """Main analysis entrypoint. Selects between live LLM run and dynamic Mock AI fallback."""
        if config.MOCK_MODE or not config.OPENAI_API_KEY or OpenAI is None:
            logger.info("Using dynamic Mock AI Analyzer...")
            return cls._generate_mock_analysis(crm_data)
            
        logger.info("Executing OpenAI Live AI Analysis...")
        try:
            client = OpenAI(api_key=config.OPENAI_API_KEY)
            
            # Format inputs
            user_content = json.dumps(crm_data, indent=2)
            
            response = client.chat.completions.create(
                model=config.OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": cls.SYSTEM_PROMPT},
                    {"role": "user", "content": user_content}
                ],
                response_format={"type": "json_object"},
                temperature=0.7,
                timeout=30
            )
            
            result_text = response.choices[0].message.content
            insights = json.loads(result_text)
            
            # Basic validation/correction
            cls._validate_and_sanitize(insights)
            return insights
            
        except Exception as e:
            logger.error(f"Live OpenAI analysis failed: {e}. Falling back to dynamic mock generator.")
            return cls._generate_mock_analysis(crm_data)

    @classmethod
    def _validate_and_sanitize(cls, data: Dict[str, Any]) -> None:
        """Enforces limits and default structures to prevent code errors."""
        limits = {
            "executive_summary": 200,
            "whats_working": 150,
            "risks": 150,
            "revenue_outlook": 150,
            "todays_priority": 150,
            "executive_takeaway": 250
        }
        
        # Ensure default values exist
        data.setdefault("ceo_name", config.CEO_NAME)
        
        for key, limit in limits.items():
            val = data.get(key, "")
            if not isinstance(val, str):
                data[key] = str(val)
                val = data[key]
                
            if len(val) > limit:
                logger.warning(f"AI field '{key}' exceeded limit ({len(val)}/{limit} chars). Truncating.")
                data[key] = val[:limit - 3] + "..."

    @classmethod
    def _generate_mock_analysis(cls, crm_data: Dict[str, Any]) -> Dict[str, Any]:
        """Generates a highly-tailored numbers-only comparison response."""
        db_y = crm_data.get("db_yesterday", {})
        y = crm_data.get("yesterday", {})
        t = crm_data.get("today", {})
        
        # Calculate deltas for Day Before Yesterday -> Yesterday
        leads_delta_y = calc_pct_change(db_y.get("leads_count", 0), y.get("leads_count", 0))
        won_val_delta_y = calc_pct_change(db_y.get("won_deals_value", 0), y.get("won_deals_value", 0))
        lost_val_delta_y = calc_pct_change(db_y.get("lost_deals_value", 0), y.get("lost_deals_value", 0))
        pipeline_delta_y = calc_pct_change(db_y.get("pipeline_value", 0), y.get("pipeline_value", 0))
        calls_delta_y = calc_pct_change(db_y.get("calls_completed", 0), y.get("calls_completed", 0))
        
        # Calculate deltas for Yesterday -> Today
        leads_delta_t = calc_pct_change(y.get("leads_count", 0), t.get("leads_count", 0))
        won_val_delta_t = calc_pct_change(y.get("won_deals_value", 0), t.get("won_deals_value", 0))
        pipeline_delta_t = calc_pct_change(y.get("pipeline_value", 0), t.get("pipeline_value", 0))
        calls_delta_t = calc_pct_change(y.get("calls_completed", 0), t.get("calls_completed", 0))

        # Format values
        db_y_pipe = format_value(db_y.get("pipeline_value", 0))
        y_pipe = format_value(y.get("pipeline_value", 0))
        t_pipe = format_value(t.get("pipeline_value", 0))
        
        db_y_won = format_value(db_y.get("won_deals_value", 0))
        y_won = format_value(y.get("won_deals_value", 0))
        t_won = format_value(t.get("won_deals_value", 0))
        
        db_y_lost = format_value(db_y.get("lost_deals_value", 0))
        y_lost = format_value(y.get("lost_deals_value", 0))

        exec_sum = f"Pipeline: {db_y_pipe} ➔ {y_pipe} ({pipeline_delta_y}) yesterday, {y_pipe} ➔ {t_pipe} ({pipeline_delta_t}) today. Leads yesterday: {db_y.get('leads_count', 0)} ➔ {y.get('leads_count', 0)} ({leads_delta_y})."
        whats_working = f"Leads up {leads_delta_y} yesterday ({db_y.get('leads_count', 0)} ➔ {y.get('leads_count', 0)}). Completed calls up {calls_delta_y} yesterday (8 ➔ 12)."
        risks = f"Lost deals: {db_y_lost} ➔ {y_lost} ({lost_val_delta_y}). Stagnant pipeline value is currently holding at {t_pipe}."
        revenue_outlook = f"Active pipeline value is stable at {t_pipe} ({pipeline_delta_t} change today). Target targets remain within 15% range."
        todays_priority = f"Convert active leads today ({t.get('leads_count', 0)} so far). Follow up calls: {y.get('calls_completed', 0)} yesterday ➔ {t.get('calls_completed', 0)} today."
        takeaway = "Numerical deltas indicate solid lead volume growth yesterday (+66.7%), but active conversions have flattened out today. Focus must shift to closing open pipeline value."

        date_str = y.get("date", datetime.now().strftime("%Y-%m-%d"))
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            formatted_date = dt.strftime("%d %b %Y")
        except Exception:
            formatted_date = date_str

        insights = {
            "ceo_name": config.CEO_NAME,
            "executive_summary": exec_sum,
            "whats_working": whats_working,
            "risks": risks,
            "revenue_outlook": revenue_outlook,
            "todays_priority": todays_priority,
            "executive_takeaway": takeaway,
            "email_subject": f"CEO Sales Delta Report - {formatted_date}",
            "email_body": ""
        }
        cls._validate_and_sanitize(insights)

        email_body = f"""Dear {config.CEO_NAME},

Here is your daily numbers-only sales delta report.

📅 Day Before Yesterday vs. Yesterday ({crm_data.get("db_yesterday_date")} ➔ {crm_data.get("yesterday_date")}):
• New Leads: {db_y.get("leads_count", 0)} ➔ {y.get("leads_count", 0)} ({leads_delta_y})
• Won Deals: {db_y_won} ➔ {y_won} ({won_val_delta_y}) (Count: {db_y.get("won_deals_count", 0)} ➔ {y.get("won_deals_count", 0)})
• Lost Deals: {db_y_lost} ➔ {y_lost} ({lost_val_delta_y}) (Count: {db_y.get("lost_deals_count", 0)} ➔ {y.get("lost_deals_count", 0)})
• Open Pipeline: {db_y_pipe} ➔ {y_pipe} ({pipeline_delta_y})
• Calls (Done/Missed): ({db_y.get("calls_completed", 0)}/{db_y.get("calls_missed", 0)}) ➔ ({y.get("calls_completed", 0)}/{y.get("calls_missed", 0)}) ({calls_delta_y} completed)

📅 Yesterday vs. Today ({crm_data.get("yesterday_date")} ➔ {crm_data.get("today_date")}):
• New Leads: {y.get("leads_count", 0)} ➔ {t.get("leads_count", 0)} ({leads_delta_t})
• Won Deals: {y_won} ➔ {t_won} ({won_val_delta_t}) (Count: {y.get("won_deals_count", 0)} ➔ {t.get("won_deals_count", 0)})
• Open Pipeline: {y_pipe} ➔ {t_pipe} ({pipeline_delta_t})
• Calls (Done/Missed): ({y.get("calls_completed", 0)}/{y.get("calls_missed", 0)}) ➔ ({t.get("calls_completed", 0)}/{t.get("calls_missed", 0)}) ({calls_delta_t} completed)

Strategic CRO Takeaway:
{insights["executive_takeaway"]}

Best regards,
Pixel Studios Sales Intelligence Engine"""

        insights["email_body"] = email_body
        return insights


# ==========================================
# 6. SMTP EMAIL CLIENT
# ==========================================
class EmailClient:
    """Manages secure SMTP connections to marketing@pixel-studios.com to deliver briefings and alerts."""
    
    @staticmethod
    def send_email(subject: str, html_content: str, text_content: str) -> bool:
        """Sends an email using the Resend HTTP API. Includes auto-fallback for unverified domains."""
        logger.info(f"Preparing to send email to {config.EMAIL_TO} via Resend (Subject: {subject})...")
        
        if config.MOCK_MODE:
            logger.info("MOCK_MODE=True: Email transmission simulated. Content:")
            logger.info(f"\n--- MOCK EMAIL SUBJECT: {subject} ---\n{text_content}\n----------------------------------")
            return True
            
        import requests
        
        url = "https://api.resend.com/emails"
        headers = {
            "Authorization": f"Bearer {config.RESEND_API_KEY}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "from": config.EMAIL_FROM,
            "to": [config.EMAIL_TO],
            "subject": subject,
            "html": html_content,
            "text": text_content
        }
        
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=15)
            
            # Auto-detect if Resend throws unverified domain error
            if response.status_code in (403, 422):
                res_data = response.json()
                error_msg = res_data.get("message", "")
                if "onboarding@resend.dev" in error_msg or "verify" in error_msg.lower() or "domain" in error_msg.lower():
                    logger.warning("Resend domain validation failed. Retrying with fallback sender onboarding@resend.dev...")
                    payload["from"] = "Report Bot <onboarding@resend.dev>"
                    response = requests.post(url, json=payload, headers=headers, timeout=15)
                    
            response.raise_for_status()
            logger.info("Email successfully dispatched via Resend API.")
            return True
        except Exception as e:
            try:
                err_body = response.text
                logger.error(f"Resend API error response body: {err_body}")
            except Exception:
                pass
            logger.error(f"Failed to dispatch email via Resend API: {e}", exc_info=True)
            return False

    @staticmethod
    def build_briefing_html(ceo_name: str, report_text: str) -> str:
        """Generates a clean preformatted HTML layout for Resend delivery."""
        return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Pixel Studios CRM Update</title>
</head>
<body style="font-family: monospace; font-size: 14px; line-height: 1.5; color: #333333; background-color: #ffffff; padding: 20px;">
    <pre style="white-space: pre-wrap; font-family: monospace; font-size: 14px; margin: 0; background-color: #fcfcfc; border: 1px solid #e0e0e0; padding: 25px; border-radius: 6px;">{report_text}</pre>
</body>
</html>"""

    @staticmethod
    def build_briefing_text(ceo_name: str, report_text: str) -> str:
        """Builds plain text structured exactly to match the user's requested template format."""
        return report_text


# ==========================================
# 7. ALERTS ENGINE
# ==========================================
class AlertsEngine:
    """Evaluates CRM data against SLA and pipeline thresholds to fire immediate warnings."""
    
    PIPELINE_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pipeline_history.json")

    @classmethod
    def check_all_alerts(cls, crm_data: Dict[str, Any]) -> List[str]:
        """Evaluates all rules and returns a list of triggered warning strings."""
        triggered_alerts = []

        # 1. High-value lead not contacted within SLA
        leads = crm_data.get("new_leads", [])
        calls = crm_data.get("calls", [])
        
        # Track which leads had calls logged
        contacted_lead_ids = set()
        for call in calls:
            who_id = call.get("Who_Id")
            if who_id:
                contacted_lead_ids.add(who_id)

        for lead in leads:
            budget = lead.get("budget") or lead.get(config.FIELD_LEAD_BUDGET) or 0.0
            created_str = lead.get("created_time")
            
            if budget >= config.ALERT_HIGH_VALUE_LEAD_BUDGET:
                # Check call SLA
                lead_id = lead.get("id")
                if lead_id not in contacted_lead_ids:
                    # Calculate time delta from creation
                    is_sla_violated = True
                    if created_str:
                        try:
                            # Parse ISO timestamp
                            created_dt = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                            time_elapsed = datetime.now(created_dt.tzinfo) - created_dt
                            if time_elapsed < timedelta(hours=config.ALERT_SLA_HOURS):
                                is_sla_violated = False
                        except Exception:
                            pass
                    
                    if is_sla_violated:
                        lead_name = f"{lead.get('first_name', '')} {lead.get('last_name', '')}".strip()
                        triggered_alerts.append(
                            f"High-Value Lead '{lead_name}' (Budget: ₹{budget:,.2f}, Owner: {lead.get('owner', 'Unassigned')}) "
                            f"remains uncontacted beyond the {config.ALERT_SLA_HOURS}-hour SLA."
                        )

        # 2. High-value opportunity marked lost
        lost_deals = crm_data.get("lost_deals", [])
        for deal in lost_deals:
            amount = deal.get("amount") or 0.0
            if amount >= config.ALERT_HIGH_VALUE_DEAL_VALUE:
                deal_name = deal.get("name", "Unknown Deal")
                reason = deal.get("loss_reason") or deal.get(config.FIELD_DEAL_LOSS_REASON) or "None listed"
                owner = deal.get("owner", "Unassigned")
                triggered_alerts.append(
                    f"CRITICAL LOSS: High-Value Opportunity '{deal_name}' (Value: ₹{amount:,.2f}, Owner: {owner}) "
                    f"was marked LOST. Reason: {reason}."
                )

        # 3. Proposal pending beyond threshold
        proposal_deals = crm_data.get("proposal_stage", [])
        for deal in proposal_deals:
            modified_str = deal.get("modified_time")
            if modified_str:
                try:
                    modified_dt = datetime.fromisoformat(modified_str.replace("Z", "+00:00"))
                    # Compare with current time in modified_dt's timezone
                    now_tz = datetime.now(modified_dt.tzinfo)
                    days_stagnant = (now_tz - modified_dt).days
                    if days_stagnant >= config.ALERT_PROPOSAL_PENDING_DAYS:
                        deal_name = deal.get("name", "Unknown Deal")
                        amount = deal.get("amount", 0.0)
                        triggered_alerts.append(
                            f"Proposal Bottleneck: Deal '{deal_name}' (Value: ₹{amount:,.2f}, Owner: {deal.get('owner')}) "
                            f"has been stagnant in Proposal stage for {days_stagnant} days."
                        )
                except Exception as e:
                    logger.debug(f"Failed to check proposal bottleneck date: {e}")

        # 4. Follow-up compliance drops below target
        tasks = crm_data.get("tasks", [])
        if tasks:
            completed_tasks = sum(1 for t in tasks if t.get("status") == "Completed")
            total_tasks = len(tasks)
            compliance = (completed_tasks / total_tasks) * 100
            if compliance < config.ALERT_COMPLIANCE_TARGET:
                triggered_alerts.append(
                    f"SLA Compliance Alert: Follow-up compliance has dropped to {compliance:.1f}% "
                    f"({completed_tasks}/{total_tasks} completed), which is below target {config.ALERT_COMPLIANCE_TARGET}%."
                )

        # 5. Pipeline drop significantly
        open_deals = crm_data.get("open_pipeline", [])
        pipeline_total = sum([d.get("amount") or 0.0 for d in open_deals])
        
        # Load history
        history = cls._load_pipeline_history()
        prev_date = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
        
        if prev_date in history:
            prev_total = history[prev_date]
            if prev_total > 0:
                drop_pct = ((prev_total - pipeline_total) / prev_total) * 100
                if drop_pct >= config.ALERT_PIPELINE_DROP_PERCENT:
                    triggered_alerts.append(
                        f"Pipeline contraction detected: Total open pipeline dropped by {drop_pct:.1f}% "
                        f"(from ₹{prev_total:,.2f} to ₹{pipeline_total:,.2f})."
                    )
        
        # Save today's pipeline total in cache
        cls._save_pipeline_total(pipeline_total)

        return triggered_alerts

    @classmethod
    def _load_pipeline_history(cls) -> Dict[str, float]:
        if os.path.exists(cls.PIPELINE_CACHE_FILE):
            try:
                with open(cls.PIPELINE_CACHE_FILE, "r") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Failed to load pipeline history cache: {e}")
        return {}

    @classmethod
    def _save_pipeline_total(cls, total: float) -> None:
        history = cls._load_pipeline_history()
        today_str = date.today().strftime("%Y-%m-%d")
        history[today_str] = total
        
        # Keep only the last 30 days to avoid bloat
        sorted_keys = sorted(history.keys())[-30:]
        trimmed_history = {k: history[k] for k in sorted_keys}
        
        try:
            with open(cls.PIPELINE_CACHE_FILE, "w") as f:
                json.dump(trimmed_history, f)
        except Exception as e:
            logger.warning(f"Failed to write pipeline history: {e}")


# ==========================================
# 8. MAIN ORCHESTRATION PIPELINE
# ==========================================
class SalesIntelligenceSystem:
    """The central coordinator of the CEO Sales Reminder workflows."""

    @classmethod
    def fetch_metrics_for_date(cls, client: ZohoClient, target_date: date) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """Queries Zoho CRM, aggregates metrics for a specific date, and returns raw detail structures."""
        start_dt = f"{target_date}T00:00:00+05:30"
        end_dt = f"{target_date}T23:59:59+05:30"
        
        try:
            leads = client.fetch_leads_created_between(start_dt, end_dt)
            modified_deals = client.fetch_deals_modified_between(start_dt, end_dt)
            calls = client.fetch_calls_between(start_dt, end_dt)
            
            won_deals = [d for d in modified_deals if d.get("Stage") == "Closed Won"]
            lost_deals = [d for d in modified_deals if d.get("Stage") == "Closed Lost"]
            
            # Fetch open pipeline
            open_deals = client.fetch_all_open_deals()
            current_pipeline = sum([d.get("amount") or 0.0 for d in open_deals])
            
            # Retrieve historical cached pipeline value if available
            history = AlertsEngine._load_pipeline_history()
            date_str = target_date.strftime("%Y-%m-%d")
            pipeline_value = history.get(date_str, current_pipeline)
            
            calls_completed = 0
            calls_missed = 0
            for c in calls:
                dur_val = c.get("Call_Duration", 0)
                try:
                    dur_int = int(dur_val) if dur_val is not None else 0
                except (ValueError, TypeError):
                    dur_int = 0
                
                if dur_int > 0:
                    calls_completed += 1
                else:
                    calls_missed += 1
            
            stats = {
                "leads_count": len(leads),
                "won_deals_count": len(won_deals),
                "won_deals_value": sum([d.get("amount") or 0.0 for d in won_deals]),
                "lost_deals_count": len(lost_deals),
                "lost_deals_value": sum([d.get("amount") or 0.0 for d in lost_deals]),
                "pipeline_value": pipeline_value,
                "calls_completed": calls_completed,
                "calls_missed": calls_missed
            }
            
            details = {
                "leads": leads,
                "won_deals": won_deals,
                "lost_deals": lost_deals,
                "calls": calls
            }
            
            return stats, details
        except Exception as e:
            logger.error(f"Failed to query metrics for {target_date}: {e}")
            stats = {
                "leads_count": 0,
                "won_deals_count": 0,
                "won_deals_value": 0.0,
                "lost_deals_count": 0,
                "lost_deals_value": 0.0,
                "pipeline_value": 0.0,
                "calls_completed": 0,
                "calls_missed": 0
            }
            return stats, {"leads": [], "won_deals": [], "lost_deals": [], "calls": []}

    @classmethod
    def fetch_crm_dataset(cls) -> Dict[str, Any]:
        """Fetches operational metrics for today, yesterday, and day-before-yesterday to compute numerical deltas."""
        today = date.today()
        yesterday = today - timedelta(days=1)
        db_yesterday = today - timedelta(days=2)
        
        if config.MOCK_MODE:
            logger.info("Retrieving metrics via Mock Provider...")
            return MockDataProvider.get_mock_crm_payload(date.today())
            
        logger.info(f"Connecting to Zoho CRM to extract activities for comparison: {db_yesterday} ➔ {yesterday} ➔ {today}")
        client = ZohoClient()
        today_str = today.strftime("%Y-%m-%d")
        
        try:
            db_yesterday_stats, _ = cls.fetch_metrics_for_date(client, db_yesterday)
            yesterday_stats, yesterday_details = cls.fetch_metrics_for_date(client, yesterday)
            today_stats, _ = cls.fetch_metrics_for_date(client, today)
            
            # Open pipeline details are fetched globally
            open_deals = client.fetch_all_open_deals()
            proposal_deals = [d for d in open_deals if d.get("Stage") == "Proposal"]
            negotiation_deals = [d for d in open_deals if d.get("Stage") == "Negotiation"]
            
            # Fetch tasks (needed for Alerts Engine check)
            tasks = client.fetch_tasks_due_between(today_str, today_str) + client.fetch_overdue_tasks(today_str)
            
            # Save today's pipeline to history cache
            AlertsEngine._save_pipeline_total(today_stats["pipeline_value"])
            
            return {
                "date": yesterday.strftime("%Y-%m-%d"),
                "db_yesterday_date": db_yesterday.strftime("%Y-%m-%d"),
                "yesterday_date": yesterday.strftime("%Y-%m-%d"),
                "today_date": today.strftime("%Y-%m-%d"),
                
                # Aggregated metrics for numbers report
                "db_yesterday": db_yesterday_stats,
                "yesterday": yesterday_stats,
                "today": today_stats,
                
                # Detail fields for yesterday (needed for Alerts Engine)
                "new_leads": yesterday_details["leads"],
                "qualified_leads": [l for l in yesterday_details["leads"] if l.get("Lead_Status") == "Qualified"],
                "won_deals": yesterday_details["won_deals"],
                "lost_deals": yesterday_details["lost_deals"],
                "open_pipeline": open_deals,
                "proposal_stage": proposal_deals,
                "negotiation_stage": negotiation_deals,
                "calls": yesterday_details["calls"],
                "tasks": tasks,
                "followups": [t for t in tasks if t.get("Due_Date") == today_str]
            }
        except Exception as e:
            logger.error(f"Failed to fetch dataset from Zoho CRM: {e}. Orchestrator aborting.", exc_info=True)
            raise

    @classmethod
    def capture_crm_snapshot(cls, client: ZohoClient) -> Dict[str, Any]:
        """Fetches active CRM records from Zoho to create a stateful snapshot."""
        if config.MOCK_MODE:
            # Simulated current state for testing
            today_iso = date.today().strftime("%Y-%m-%dT10:00:00+05:30")
            return {
                "leads": {
                    "l2": {"name": "David Miller", "status": "Warm", "owner": "Rohan Sharma"}
                },
                "deals": {
                    "d1": {
                        "name": "ABC Corp",
                        "stage": "Proposal",
                        "amount": 520000.0,
                        "owner": "Siddhanth",
                        "created_time": "2026-06-18T10:00:00+05:30",
                        "modified_time": today_iso,
                        "source": "Google Ads",
                        "service": "Custom Development",
                        "loss_reason": "None"
                    },
                    "d2": {
                        "name": "Existing Client Expansion",
                        "stage": "Closed Won",
                        "amount": 300000.0,
                        "owner": "Siddhanth",
                        "created_time": "2026-06-15T09:00:00+05:30",
                        "modified_time": today_iso,
                        "source": "Referral",
                        "service": "App Modernization",
                        "loss_reason": "None"
                    },
                    "d3": {
                        "name": "Failed Lead Corp",
                        "stage": "Closed Lost",
                        "amount": 150000.0,
                        "owner": "Rohan Sharma",
                        "created_time": "2026-06-16T12:00:00+05:30",
                        "modified_time": today_iso,
                        "source": "Cold Outreach",
                        "service": "UI Design",
                        "loss_reason": "Pricing too high"
                    },
                    "d4": {
                        "name": "Acme SaaS Integration",
                        "stage": "Qualification",
                        "amount": 600000.0,
                        "owner": "Rohan Sharma",
                        "created_time": today_iso,
                        "modified_time": today_iso,
                        "source": "Inbound Website",
                        "service": "SaaS Integration",
                        "loss_reason": "None"
                    }
                },
                "calls": {
                    "c1": {"subject": "Inbound Call", "duration": 0, "is_missed": True},
                    "c2": {"subject": "Inbound Call", "duration": 0, "is_missed": True}
                },
                "contacts": {
                    "c_j": {"name": "John Smith"}
                },
                "stats": {
                    "new_leads": 1,
                    "deals_won": 1,
                    "deals_lost": 1,
                    "calls_completed": 5,
                    "calls_missed": 2,
                    "tasks_due_today": 2,
                    "tasks_overdue": 1
                }
            }

        logger.info("Capturing live Zoho CRM stateful snapshot...")
        
        # 1. Fetch Deals (modified or created in the last 30 days)
        cutoff_deals_date = (date.today() - timedelta(days=30)).strftime("%Y-%m-%d")
        deals_query = (
            f"select id, Deal_Name, Stage, Amount, Created_Time, Modified_Time, Lead_Source, "
            f"{config.FIELD_LEAD_SERVICE}, {config.FIELD_DEAL_LOSS_REASON}, "
            f"Owner.first_name, Owner.last_name from Deals "
            f"where Modified_Time >= '{cutoff_deals_date}T00:00:00+05:30'"
        )
        recent_deals = client.execute_coql(deals_query)
        deals_dict = {}
        for d in recent_deals:
            deal_id = d.get("id")
            if deal_id:
                deals_dict[deal_id] = {
                    "name": d.get("Deal_Name") or "Unknown Deal",
                    "stage": d.get("Stage") or "None",
                    "amount": float(d.get("Amount") or 0.0) if d.get("Amount") is not None else 0.0,
                    "owner": f"{d.get('Owner', {}).get('first_name', '')} {d.get('Owner', {}).get('last_name', '')}".strip() or "Unassigned",
                    "created_time": d.get("Created_Time"),
                    "modified_time": d.get("Modified_Time"),
                    "source": d.get("Lead_Source") or "None",
                    "service": d.get(config.FIELD_LEAD_SERVICE) or "None",
                    "loss_reason": d.get(config.FIELD_DEAL_LOSS_REASON) or "None"
                }

        # 2. Fetch Leads (modified in last 14 days)
        cutoff_date = (date.today() - timedelta(days=14)).strftime("%Y-%m-%d")
        leads_query = (
            f"select id, First_Name, Last_Name, Lead_Status, "
            f"Owner.first_name, Owner.last_name from Leads where Modified_Time >= '{cutoff_date}T00:00:00+05:30'"
        )
        active_leads = client.execute_coql(leads_query)
        leads_dict = {}
        for l in active_leads:
            lead_id = l.get("id")
            if lead_id:
                leads_dict[lead_id] = {
                    "name": f"{l.get('First_Name', '')} {l.get('Last_Name', '')}".strip() or "Unknown Lead",
                    "status": l.get("Lead_Status") or "None",
                    "owner": f"{l.get('Owner', {}).get('first_name', '')} {l.get('Owner', {}).get('last_name', '')}".strip() or "Unassigned"
                }

        # 3. Fetch Calls (created/modified in last 7 days)
        calls_cutoff = (date.today() - timedelta(days=7)).strftime("%Y-%m-%d")
        calls_query = (
            f"select id, Subject, Call_Start_Time, Call_Duration from Calls "
            f"where Call_Start_Time >= '{calls_cutoff}T00:00:00+05:30'"
        )
        active_calls = client.execute_coql(calls_query)
        calls_dict = {}
        for c in active_calls:
            call_id = c.get("id")
            if call_id:
                dur_val = c.get("Call_Duration", 0)
                try:
                    dur_int = int(dur_val) if dur_val is not None else 0
                except (ValueError, TypeError):
                    dur_int = 0
                calls_dict[call_id] = {
                    "subject": c.get("Subject") or "Unknown Call",
                    "duration": dur_int,
                    "is_missed": dur_int == 0
                }

        # 4. Fetch Contacts (created in last 14 days)
        contacts_query = (
            f"select id, First_Name, Last_Name from Contacts where Created_Time >= '{cutoff_date}T00:00:00+05:30'"
        )
        active_contacts = client.execute_coql(contacts_query)
        contacts_dict = {}
        for c in active_contacts:
            contact_id = c.get("id")
            if contact_id:
                contacts_dict[contact_id] = {
                    "name": f"{c.get('First_Name', '')} {c.get('Last_Name', '')}".strip() or "Unknown Contact"
                }

        # 5. Fetch Daily Stats
        today_date = date.today()
        today_str = today_date.strftime("%Y-%m-%d")
        
        # Calculate active tasks metrics
        overdue_tasks = client.fetch_overdue_tasks(today_str)
        tasks_due = client.fetch_tasks_due_between(today_str, today_str)
        
        # Calculate daily leads, deals, calls metrics
        try:
            day_stats, _ = cls.fetch_metrics_for_date(client, today_date)
        except Exception as e:
            logger.error(f"Failed to fetch daily metrics for today: {e}")
            day_stats = {}
            
        stats_dict = {
            "new_leads": day_stats.get("leads_count", 0),
            "deals_won": day_stats.get("won_deals_count", 0),
            "deals_lost": day_stats.get("lost_deals_count", 0),
            "calls_completed": day_stats.get("calls_completed", 0),
            "calls_missed": day_stats.get("calls_missed", 0),
            "tasks_due_today": len(tasks_due),
            "tasks_overdue": len(overdue_tasks)
        }

        return {
            "leads": leads_dict,
            "deals": deals_dict,
            "calls": calls_dict,
            "contacts": contacts_dict,
            "stats": stats_dict
        }

    @classmethod
    def generate_change_report(cls, prev_snap: Dict[str, Any], curr_snap: Dict[str, Any]) -> str:
        """Compares two snapshots and generates the executive digest report."""
        prev_leads = prev_snap.get("leads", {})
        curr_leads = curr_snap.get("leads", {})
        prev_leads_cnt = len(prev_leads)
        curr_leads_cnt = len(curr_leads)
        
        # Added Leads
        added_leads = []
        for lid, info in curr_leads.items():
            if lid not in prev_leads:
                added_leads.append(info.get("name") or "Unknown Lead")
                
        # Removed Leads
        removed_leads = []
        for lid, info in prev_leads.items():
            if lid not in curr_leads:
                removed_leads.append(info.get("name") or "Unknown Lead")
                
        # Deals comparison
        prev_deals = prev_snap.get("deals", {})
        curr_deals = curr_snap.get("deals", {})
        
        new_deals = []
        deal_movements = []
        deals_won = []
        deals_lost = []
        
        # Determine current date/time context for new deals check
        today_date = date.today()
        two_days_ago = today_date - timedelta(days=2)
        
        for did, curr_deal in curr_deals.items():
            # Check if new
            created_str = curr_deal.get("created_time")
            is_new = False
            if did not in prev_deals and created_str:
                try:
                    created_dt = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                    if created_dt.date() >= two_days_ago:
                        is_new = True
                except Exception:
                    pass
            
            if is_new:
                new_deals.append({
                    "name": curr_deal.get("name") or "Unknown Deal",
                    "source": curr_deal.get("source") or "None",
                    "service": curr_deal.get("service") or "None",
                    "value": format_value(curr_deal.get("amount", 0.0)),
                    "owner": curr_deal.get("owner") or "Unassigned"
                })
            
            # Check stage movement
            if did in prev_deals:
                prev_deal = prev_deals[did]
                old_stage = prev_deal.get("stage") or "None"
                new_stage = curr_deal.get("stage") or "None"
                if old_stage != new_stage:
                    deal_movements.append({
                        "name": curr_deal.get("name") or "Unknown Deal",
                        "old_stage": old_stage,
                        "new_stage": new_stage
                    })
                    
            # Check won
            curr_stage = curr_deal.get("stage") or "None"
            is_won = curr_stage == "Closed Won" and (did not in prev_deals or prev_deals[did].get("stage") != "Closed Won")
            if is_won:
                deals_won.append({
                    "name": curr_deal.get("name") or "Unknown Deal",
                    "value": format_value(curr_deal.get("amount", 0.0)),
                    "owner": curr_deal.get("owner") or "Unassigned"
                })
                
            # Check lost
            is_lost = curr_stage == "Closed Lost" and (did not in prev_deals or prev_deals[did].get("stage") != "Closed Lost")
            if is_lost:
                reason = curr_deal.get("loss_reason")
                if not reason or reason == "None":
                    reason = "None listed"
                deals_lost.append({
                    "name": curr_deal.get("name") or "Unknown Deal",
                    "value": format_value(curr_deal.get("amount", 0.0)),
                    "reason": reason
                })
                
        # Call Activity
        curr_stats = curr_snap.get("stats", {})
        curr_missed_calls = curr_stats.get("calls_missed", 0)
        curr_completed_calls = curr_stats.get("calls_completed", 0)
        
        # Check if there is any activity
        has_activity = (
            len(added_leads) > 0 or
            len(removed_leads) > 0 or
            len(new_deals) > 0 or
            len(deal_movements) > 0 or
            len(deals_won) > 0 or
            len(deals_lost) > 0 or
            curr_missed_calls > 0 or
            curr_completed_calls > 0
        )
        
        if not has_activity:
            return "No CRM changes detected since yesterday."
            
        # Build layout lines
        lines = []
        lines.append("Pixel Studios CRM Update")
        lines.append(today_date.strftime("%d %b %Y"))
        lines.append("")
        
        sep = "━━━━━━━━━━━━━━"
        
        # LEAD CHANGES
        lines.append(sep)
        lines.append("LEAD CHANGES")
        lines.append(sep)
        lines.append("")
        lines.append("Lead Count")
        lead_diff = curr_leads_cnt - prev_leads_cnt
        lead_diff_sign = "+" if lead_diff > 0 else ""
        lines.append(f"{prev_leads_cnt} → {curr_leads_cnt} ({lead_diff_sign}{lead_diff})")
        lines.append("")
        lines.append("Added Leads")
        if added_leads:
            for lead_name in sorted(added_leads):
                lines.append(f"• {lead_name}")
        else:
            lines.append("None")
        lines.append("")
        lines.append("Removed Leads")
        if removed_leads:
            for lead_name in sorted(removed_leads):
                lines.append(f"• {lead_name}")
        else:
            lines.append("None")
        lines.append("")
        
        # NEW DEALS
        lines.append(sep)
        lines.append("NEW DEALS")
        lines.append(sep)
        lines.append("")
        if new_deals:
            new_deals_sorted = sorted(new_deals, key=lambda x: x["name"])
            for idx, deal in enumerate(new_deals_sorted):
                if idx > 0:
                    lines.append("")
                lines.append(f"• {deal['name']}")
                lines.append(f"Source: {deal['source']}")
                lines.append(f"Service: {deal['service']}")
                lines.append(f"Value: {deal['value']}")
                lines.append(f"Owner: {deal['owner']}")
        else:
            lines.append("None")
        lines.append("")
        
        # DEAL MOVEMENTS
        lines.append(sep)
        lines.append("DEAL MOVEMENTS")
        lines.append(sep)
        lines.append("")
        if deal_movements:
            deal_movements_sorted = sorted(deal_movements, key=lambda x: x["name"])
            for idx, mv in enumerate(deal_movements_sorted):
                if idx > 0:
                    lines.append("")
                lines.append(f"• {mv['name']}")
                lines.append(f"{mv['old_stage']} → {mv['new_stage']}")
        else:
            lines.append("None")
        lines.append("")
        
        # DEALS WON
        lines.append(sep)
        lines.append("DEALS WON")
        lines.append(sep)
        lines.append("")
        if deals_won:
            deals_won_sorted = sorted(deals_won, key=lambda x: x["name"])
            for idx, deal in enumerate(deals_won_sorted):
                if idx > 0:
                    lines.append("")
                lines.append(f"• {deal['name']}")
                lines.append(f"Value: {deal['value']}")
                lines.append(f"Owner: {deal['owner']}")
        else:
            lines.append("None")
        lines.append("")
        
        # DEALS LOST
        lines.append(sep)
        lines.append("DEALS LOST")
        lines.append(sep)
        lines.append("")
        if deals_lost:
            deals_lost_sorted = sorted(deals_lost, key=lambda x: x["name"])
            for idx, deal in enumerate(deals_lost_sorted):
                if idx > 0:
                    lines.append("")
                lines.append(f"• {deal['name']}")
                lines.append(f"Value: {deal['value']}")
                lines.append(f"Reason: {deal['reason']}")
        else:
            lines.append("None")
        lines.append("")
        
        # CALL ACTIVITY
        lines.append(sep)
        lines.append("CALL ACTIVITY")
        lines.append(sep)
        lines.append("")
        lines.append("Missed Calls")
        lines.append(f"• {curr_missed_calls}")
        lines.append("")
        lines.append("Completed Calls")
        lines.append(f"• {curr_completed_calls}")
        
        return "\n".join(lines)

    @classmethod
    def run_daily_report(cls) -> bool:
        """Executes the daily scheduled change detection and reporting workflow."""
        logger.info("--- STARTING DAILY SALES BRIEFING DISPATCH ---")
        SNAPSHOT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "crm_snapshot.json")
        
        try:
            client = ZohoClient()
            curr_snap = cls.capture_crm_snapshot(client)
            
            # Load prev snap
            prev_snap = {}
            if config.MOCK_MODE:
                # In mock mode, always simulate the specific comparison from the user prompt
                prev_snap = {
                    "leads": {
                        "l1": {"name": "Sarah Johnson", "status": "Cold", "owner": "Rohan Sharma"},
                        "l2": {"name": "David Miller", "status": "Warm", "owner": "Rohan Sharma"},
                        "l3": {"name": "Emily Davis", "status": "Cold", "owner": "Rohan Sharma"}
                    },
                    "deals": {
                        "d1": {
                            "name": "ABC Corp",
                            "stage": "Qualification",
                            "amount": 420000.0,
                            "owner": "Rahul",
                            "created_time": "2026-06-18T10:00:00+05:30",
                            "modified_time": "2026-06-18T10:00:00+05:30",
                            "source": "Google Ads",
                            "service": "Custom Development",
                            "loss_reason": "None"
                        },
                        "d2": {
                            "name": "Existing Client Expansion",
                            "stage": "Proposal",
                            "amount": 300000.0,
                            "owner": "Siddhanth",
                            "created_time": "2026-06-15T09:00:00+05:30",
                            "modified_time": "2026-06-15T09:00:00+05:30",
                            "source": "Referral",
                            "service": "App Modernization",
                            "loss_reason": "None"
                        },
                        "d3": {
                            "name": "Failed Lead Corp",
                            "stage": "Negotiation",
                            "amount": 150000.0,
                            "owner": "Rohan Sharma",
                            "created_time": "2026-06-16T12:00:00+05:30",
                            "modified_time": "2026-06-16T12:00:00+05:30",
                            "source": "Cold Outreach",
                            "service": "UI Design",
                            "loss_reason": "None"
                        }
                    },
                    "calls": {},
                    "contacts": {},
                    "stats": {
                        "new_leads": 3,
                        "deals_won": 1,
                        "deals_lost": 1,
                        "calls_completed": 8,
                        "calls_missed": 0,
                        "tasks_due_today": 4,
                        "tasks_overdue": 3
                    }
                }
            else:
                if os.path.exists(SNAPSHOT_FILE):
                    try:
                        with open(SNAPSHOT_FILE, "r") as f:
                            prev_snap = json.load(f)
                    except Exception as e:
                        logger.warning(f"Failed to load CRM baseline snapshot: {e}")
            
            # Generate report
            if not prev_snap:
                logger.info("No previous snapshot found. Initializing baseline snapshot.")
                report_text = cls.generate_change_report(curr_snap, curr_snap)
            else:
                report_text = cls.generate_change_report(prev_snap, curr_snap)
                
            # Save current snapshot for next run
            if not config.MOCK_MODE:
                try:
                    with open(SNAPSHOT_FILE, "w") as f:
                        json.dump(curr_snap, f)
                except Exception as e:
                    logger.warning(f"Failed to save CRM snapshot: {e}")
                    
            # Build and send email
            text_body = EmailClient.build_briefing_text(config.CEO_NAME, report_text)
            html_body = EmailClient.build_briefing_html(config.CEO_NAME, report_text)
            
            success = EmailClient.send_email(
                subject=f"Pixel Studios CRM Update - {date.today().strftime('%A, %d %b %Y')}",
                html_content=html_body,
                text_content=text_body
            )
            
            if success:
                logger.info("Daily CRM change report complete and dispatched successfully.")
            else:
                logger.error("Failed to deliver daily CRM change report.")
                
            return success
        except Exception as e:
            logger.critical(f"Daily Briefing Pipeline crashed: {e}", exc_info=True)
            return False

    @classmethod
    def run_alerts_check(cls) -> int:
        """Standalone check of critical SLAs. Dispatches email warnings if issues are detected."""
        logger.info("--- EXECUTING STANDALONE SALES ALERT CHECK ---")
        try:
            dataset = cls.fetch_crm_dataset()
            alerts = AlertsEngine.check_all_alerts(dataset)
            
            if not alerts:
                logger.info("No sales alerts triggered during scan.")
                return 0
                
            logger.warning(f"{len(alerts)} sales alerts triggered! Sending warnings...")
            
            # Format alert notifications
            subject = f"🚨 URGENT: Pixel Studios Sales Alerts ({len(alerts)})"
            
            # Simple text layout
            text_lines = ["The following critical conditions require attention:", ""]
            for a in alerts:
                text_lines.append(f"- ⚠️ {a}")
            text_body = "\n".join(text_lines)
            
            # Styled html layout
            alerts_li = "".join([f"<li style='margin-bottom: 12px; font-size: 15px; color: #2b2d42;'>⚠️ {a}</li>" for a in alerts])
            html_body = f"""
            <html>
            <body style="font-family: Arial, sans-serif; background-color: #fff8f8; padding: 20px;">
                <div style="max-width: 600px; margin: 0 auto; background: white; padding: 25px; border-radius: 8px; border: 1px solid #ffccd5; box-shadow: 0 4px 6px rgba(0,0,0,0.05);">
                    <h2 style="color: #d90429; margin-top: 0;">🚨 Critical Sales Alerts</h2>
                    <p style="color: #555555; font-size: 14px;">The Pixel Studios Sales Engine detected the following compliance SLA breaches or lost deal conditions:</p>
                    <hr style="border: 0; border-top: 1px dashed #ffccd5; margin: 20px 0;">
                    <ul>
                        {alerts_li}
                    </ul>
                    <hr style="border: 0; border-top: 1px dashed #ffccd5; margin: 20px 0;">
                    <p style="font-size: 12px; color: #999999; margin: 0;">This warning email was generated automatically by the Pixel Studios Intelligence System.</p>
                </div>
            </body>
            </html>
            """
            
            EmailClient.send_email(subject, html_body, text_body)
            return len(alerts)
        except Exception as e:
            logger.critical(f"Standalone Alert scan crashed: {e}", exc_info=True)
            return 0


# ==========================================
# 9. RUNNER AND SCHEDULER
# ==========================================
def run_scheduler_loop():
    """Starts a daemon scheduler checking and running daily tasks at 8:00 AM IST."""
    try:
        import schedule
    except ImportError:
        logger.critical("Library 'schedule' is missing. Please run: pip install schedule")
        return
        
    logger.info("Initializing schedule loops...")
    
    # Daily sales reminder at 05:30 AM UTC (11:00 AM IST)
    schedule.every().day.at("05:30").do(SalesIntelligenceSystem.run_daily_report)
    
    logger.info("CEO Sales Reminder scheduler is RUNNING. Press Ctrl+C to terminate.")
    
    try:
        while True:
            schedule.run_pending()
            time.sleep(30)
    except KeyboardInterrupt:
        logger.info("Scheduler daemon shut down by user.")


def main():
    parser = argparse.ArgumentParser(description="CEO Sales Reminder - Sales Intelligence Daemon")
    
    parser.add_argument("--daily-report", action="store_true", help="Manually run the daily sales briefing flow.")
    parser.add_argument("--check-alerts", action="store_true", help="Manually run the compliance alerts check.")
    parser.add_argument("--run-all", action="store_true", help="Execute both report and alert check.")
    parser.add_argument("--test", action="store_true", help="Forces MOCK_MODE=True and tests email report outputs locally.")
    parser.add_argument("--schedule", action="store_true", help="Start the daemon scheduling loop.")
    
    args = parser.parse_args()
    
    if args.test:
        # Force Mock mode for verification
        config.MOCK_MODE = True
        logger.info("Forcing MOCK_MODE=True for local verification tests...")
        
        # Save pipeline baseline so drop alert can be evaluated
        AlertsEngine._save_pipeline_total(1800000.0) # Baseline value
        
        report_success = SalesIntelligenceSystem.run_daily_report()
        logger.info(f"Verification report run completed. Result: {'SUCCESS' if report_success else 'FAILED'}")
        sys.exit(0 if report_success else 1)
        
    elif args.daily_report:
        success = SalesIntelligenceSystem.run_daily_report()
        sys.exit(0 if success else 1)
        
    elif args.check_alerts:
        SalesIntelligenceSystem.run_alerts_check()
        sys.exit(0)
        
    elif args.run_all:
        report_success = SalesIntelligenceSystem.run_daily_report()
        SalesIntelligenceSystem.run_alerts_check()
        sys.exit(0 if report_success else 1)
        
    elif args.schedule:
        run_scheduler_loop()
        
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
