"""Smart Employee Onboarding & Identity Service - all Lambda handlers."""
import json, os, re, uuid, secrets, decimal, textwrap, traceback, urllib.parse
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timezone

import boto3
from botocore.config import Config
from boto3.dynamodb.conditions import Key

REGION = os.environ.get("AWS_REGION", "ap-south-1")
ddb = boto3.resource("dynamodb")
EMP = ddb.Table(os.environ["EMP_TABLE"])
STG = ddb.Table(os.environ["STAGE_TABLE"])
s3 = boto3.client(
    "s3", region_name=REGION, endpoint_url=f"https://s3.{REGION}.amazonaws.com",
    config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
)
ses, sns, sfn, cog = (boto3.client(x) for x in ("ses", "sns", "stepfunctions", "cognito-idp"))

BUCKET, SENDER, TOPIC = os.environ["DOC_BUCKET"], os.environ["SENDER"], os.environ["TOPIC_ARN"]
POOL, SM_ARN, SITE = os.environ["POOL_ID"], os.environ["SM_ARN"], os.environ["SITE_URL"]
REMIND = max(int(os.environ.get("REMINDER_SECONDS", "86400")), 86400)   # never more often than every 24h
DOMAIN = os.environ.get("COMPANY_DOMAIN", "company.example")

STAGES = ["DOCUMENTS", "IT_PROVISIONING", "POLICY_SIGNOFF", "MANAGER_INTRO"]
DOC_TYPES = ["ID_PROOF", "DEGREE_CERTIFICATE", "SIGNED_OFFER_LETTER"]
ALLOWED_EXT = {"pdf", "jpg", "jpeg", "png"}
MAX_BYTES = 5 * 1024 * 1024
FIELDS = ("name", "email", "department", "role", "manager", "joining_date", "employment_type")


# ------------------------------------------------------------------ helpers
class Enc(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, decimal.Decimal):
            return int(o) if o == int(o) else float(o)
        return super().default(o)


def resp(code, body):
    return {"statusCode": code, "headers": {"Content-Type": "application/json"},
            "body": json.dumps(body, cls=Enc)}


def now():
    return datetime.now(timezone.utc).isoformat()


def mail(to, subject, html):
    try:
        ses.send_email(Source=SENDER, Destination={"ToAddresses": [to]},
                       Message={"Subject": {"Data": subject}, "Body": {"Html": {"Data": html}}})
        return True
    except Exception as e:  # SES sandbox: recipient must be verified
        print("SES failed:", e)
        return False


def make_pdf(items):
    """Tiny dependency-free PDF writer. items = [(bold?, size, text), ...]"""
    esc = lambda s: s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)").encode("latin-1", "replace")
    stream, y = b"", 790
    for bold, size, text in items:
        if text:
            stream += b"BT /F%d %d Tf 60 %d Td (" % (2 if bold else 1, size, y) + esc(text) + b") Tj ET\n"
        y -= size + 9
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R /F2 6 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"endstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>"]
    out, offs = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offs)
    return out + b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % (len(objs) + 1, xref)


def offer_letter_pdf(emp):
    body = (f"We are delighted to offer you the position of {emp['role']} in the {emp['department']} "
            f"department, reporting to {emp['manager']}. This is a {emp['employment_type']} position and your "
            f"joining date is {emp['joining_date']}.")
    steps = ["Next steps:", f"1. Sign in to the onboarding portal: {SITE}",
             "2. Upload your ID proof, degree certificate and a signed copy of this letter.",
             "3. Accept the company policies and meet your manager."]
    items = [(1, 22, "OFFER OF EMPLOYMENT"), (0, 11, datetime.now(timezone.utc).strftime("%d %B %Y")), (0, 11, ""),
             (1, 13, f"Dear {emp['name']},"), (0, 11, "")]
    items += [(0, 11, l) for l in textwrap.wrap(body, 88)] + [(0, 11, "")]
    items += [(1 if i == 0 else 0, 11, l) for i, s in enumerate(steps) for l in textwrap.wrap(s, 88)]
    items += [(0, 11, ""), (0, 11, "Please print, sign and upload this letter as 'Signed Offer Letter' in the portal."),
              (0, 11, ""), (0, 11, "We look forward to welcoming you to the team!"), (0, 11, ""),
              (1, 11, "HR Team"), (0, 9, ""), (0, 9, f"Employee reference: {emp['employee_id']}"),
              (0, 9, "Signature: ______________________     Date: ______________")]
    return make_pdf(items)


def send_offer(emp):
    """Email the offer letter PDF as an attachment (SES raw email)."""
    try:
        msg = MIMEMultipart("mixed")
        msg["Subject"], msg["From"], msg["To"] = "Your offer letter 🎉", SENDER, emp["email"]
        msg.attach(MIMEText(f"<h2>Congratulations, {emp['name']}!</h2><p>Your offer letter is attached. "
                            f"Please sign it and upload it in the onboarding portal: <a href='{SITE}'>{SITE}</a></p>", "html"))
        part = MIMEApplication(offer_letter_pdf(emp), _subtype="pdf")
        part.add_header("Content-Disposition", "attachment", filename="Offer_Letter.pdf")
        msg.attach(part)
        ses.send_raw_email(Source=SENDER, Destinations=[emp["email"]], RawMessage={"Data": msg.as_bytes()})
        return True
    except Exception as e:
        print("SES offer failed:", e)
        return False


def alert_hr(subject, text):
    try:
        sns.publish(TopicArn=TOPIC, Subject=subject[:99], Message=text)
    except Exception as e:
        print("SNS failed:", e)


def get_emp(eid):
    return EMP.get_item(Key={"employee_id": eid}).get("Item")


def get_stage(eid, stage):
    return STG.get_item(Key={"employee_id": eid, "stage": stage}).get("Item", {})


def finish_stage(eid, stage):
    """Mark a stage complete and release the Step Functions task token."""
    old = STG.update_item(
        Key={"employee_id": eid, "stage": stage},
        UpdateExpression="SET #s = :c, completed_at = :t REMOVE task_token",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":c": "complete", ":t": now()},
        ReturnValues="UPDATED_OLD",
    ).get("Attributes", {})
    token = old.get("task_token")
    if token:
        try:
            sfn.send_task_success(taskToken=token, output="{}")
        except Exception as e:
            print("send_task_success:", e)


def progress(eid):
    emp = get_emp(eid)
    if not emp:
        return None
    items = STG.query(KeyConditionExpression=Key("employee_id").eq(eid))["Items"]
    by = {i["stage"]: i for i in items}
    stages = [{"stage": s, "status": by.get(s, {}).get("status", "pending"),
               "completed_at": by.get(s, {}).get("completed_at")} for s in STAGES]
    done = sum(1 for s in stages if s["status"] == "complete")
    docs = by.get("DOCUMENTS", {})
    return {**emp, "stages": stages, "percent": done * 25,
            "documents": docs.get("docs", {}), "missing": docs.get("missing", DOC_TYPES)}


# ------------------------------------------------------------------ API
def create_employee(b):
    for f in FIELDS:
        if not str(b.get(f, "")).strip():
            return resp(400, {"error": f"{f} is required"})
    eid, email = str(uuid.uuid4()), b["email"].strip().lower()
    temp = "Tmp#" + secrets.token_urlsafe(6) + "a1A"
    try:
        cog.admin_create_user(
            UserPoolId=POOL, Username=email, TemporaryPassword=temp, MessageAction="SUPPRESS",
            UserAttributes=[{"Name": "email", "Value": email}, {"Name": "email_verified", "Value": "true"},
                            {"Name": "name", "Value": b["name"]}, {"Name": "custom:employee_id", "Value": eid}])
    except cog.exceptions.UsernameExistsException:
        return resp(409, {"error": "An account with this email already exists"})
    cog.admin_add_user_to_group(UserPoolId=POOL, Username=email, GroupName="Employees")

    EMP.put_item(Item={"employee_id": eid, **{f: b[f].strip() for f in FIELDS if f != "email"},
                       "email": email, "overall_status": "ONBOARDING", "created_at": now()})
    for s in STAGES:
        item = {"employee_id": eid, "stage": s, "status": "pending"}
        if s == "DOCUMENTS":
            item.update(docs={}, missing=DOC_TYPES)
        STG.put_item(Item=item)

    sent = mail(email, "Welcome aboard! Your onboarding account is ready",
                f"<h2>Welcome, {b['name']}!</h2><p>Sign in to complete your onboarding:</p>"
                f"<p><a href='{SITE}'>{SITE}</a></p><p><b>Username:</b> {email}<br>"
                f"<b>Temporary password:</b> {temp}</p><p>You will be asked to set a new password.</p>")
    sfn.start_execution(stateMachineArn=SM_ARN, name=eid,
                        input=json.dumps({"employee_id": eid, "reminder_seconds": REMIND}))
    return resp(201, {"employee_id": eid, "email_sent": sent})


def list_employees():
    emps, stg = EMP.scan()["Items"], STG.scan()["Items"]
    grouped = {}
    for s in stg:
        grouped.setdefault(s["employee_id"], {})[s["stage"]] = s["status"]
    for e in emps:
        st = grouped.get(e["employee_id"], {})
        e["stages"] = [{"stage": s, "status": st.get(s, "pending")} for s in STAGES]
        e["percent"] = 25 * sum(1 for s in e["stages"] if s["status"] == "complete")
    return sorted(emps, key=lambda e: e.get("created_at", ""), reverse=True)


def upload_url(eid, b):
    doc_type, name = b.get("doc_type"), re.sub(r"[^A-Za-z0-9._-]", "_", b.get("filename", "file"))
    if doc_type not in DOC_TYPES:
        return resp(400, {"error": "Invalid doc_type"})
    if name.rsplit(".", 1)[-1].lower() not in ALLOWED_EXT:
        return resp(400, {"error": "Only PDF, JPG or PNG files are allowed"})
    post = s3.generate_presigned_post(
        BUCKET, f"{eid}/{doc_type}/{name}",
        Conditions=[["content-length-range", 1, MAX_BYTES]], ExpiresIn=300)
    return resp(200, post)


def complete_stage(eid, stage, is_hr):
    if stage == "MANAGER_INTRO" and not is_hr:
        return resp(403, {"error": "Only HR can confirm the manager intro"})
    if stage not in ("POLICY_SIGNOFF", "MANAGER_INTRO"):
        return resp(400, {"error": "This stage completes automatically"})
    if get_stage(eid, stage).get("status") != "in_progress":
        return resp(409, {"error": "This stage is not active yet"})
    finish_stage(eid, stage)
    return resp(200, {"ok": True})


def release_offer(eid, is_hr):
    """HR releases the offer letter - only after the documents are verified."""
    if not is_hr:
        return resp(403, {"error": "HR admin only"})
    emp = get_emp(eid)
    if not emp:
        return resp(404, {"error": "Employee not found"})
    if get_stage(eid, "DOCUMENTS").get("status") != "complete":
        return resp(409, {"error": "Documents are not verified yet"})
    ok = send_offer(emp)
    if ok:
        EMP.update_item(Key={"employee_id": eid}, UpdateExpression="SET offer_released_at = :t",
                        ExpressionAttributeValues={":t": now()})
    return resp(200, {"offer_sent": ok})


def delete_employee(eid):
    """Remove everything about an employee: workflow, login, files and records."""
    emp = get_emp(eid)
    if not emp:
        return resp(404, {"error": "Employee not found"})
    try:
        sfn.stop_execution(executionArn=SM_ARN.replace(":stateMachine:", ":execution:") + ":" + eid,
                           cause="Employee removed by HR")
    except Exception as e:
        print("stop_execution:", e)
    try:
        cog.admin_delete_user(UserPoolId=POOL, Username=emp["email"])
    except Exception as e:
        print("admin_delete_user:", e)
    kw = {"Bucket": BUCKET, "Prefix": eid + "/"}
    while True:
        r = s3.list_objects_v2(**kw)
        objs = [{"Key": o["Key"]} for o in r.get("Contents", [])]
        if objs:
            s3.delete_objects(Bucket=BUCKET, Delete={"Objects": objs})
        if not r.get("IsTruncated"):
            break
        kw["ContinuationToken"] = r["NextContinuationToken"]
    for it in STG.query(KeyConditionExpression=Key("employee_id").eq(eid))["Items"]:
        STG.delete_item(Key={"employee_id": eid, "stage": it["stage"]})
    EMP.delete_item(Key={"employee_id": eid})
    return resp(200, {"deleted": True})


def api_handler(event, _):
    try:
        rc = event["requestContext"]
        method, path = rc["http"]["method"], event["rawPath"].strip("/")
        claims = rc["authorizer"]["jwt"]["claims"]
        is_hr = "HRAdmin" in str(claims.get("cognito:groups", ""))
        me = claims.get("custom:employee_id")
        body = json.loads(event.get("body") or "{}")
        parts = path.split("/")
        if parts[0] != "employees":
            return resp(404, {"error": "Not found"})
        if len(parts) == 1:
            if not is_hr:
                return resp(403, {"error": "HR admin only"})
            if method == "POST":
                return create_employee(body)
            return resp(200, list_employees())
        eid, tail = parts[1], parts[2:]
        if not (is_hr or me == eid):
            return resp(403, {"error": "Forbidden"})
        if method == "GET" and tail == ["progress"]:
            p = progress(eid)
            return resp(200, p) if p else resp(404, {"error": "Employee not found"})
        if method == "POST" and tail == ["upload-url"]:
            return upload_url(eid, body)
        if method == "POST" and tail == ["offer-letter"]:
            return release_offer(eid, is_hr)
        if method == "DELETE" and not tail:
            return delete_employee(eid) if is_hr else resp(403, {"error": "HR admin only"})
        if method == "POST" and len(tail) == 3 and tail[0] == "stages" and tail[2] == "complete":
            return complete_stage(eid, tail[1], is_hr)
        return resp(404, {"error": "Not found"})
    except Exception as e:
        traceback.print_exc()
        return resp(500, {"error": str(e)})


# ------------------------------------------------------------------ workflow tasks
def stage_start(e, _):
    eid, stage, token = e["employee_id"], e["stage"], e["token"]
    cur = get_stage(eid, stage)
    if cur.get("status") == "complete":           # finished before the stage became active
        sfn.send_task_success(taskToken=token, output="{}")
        return
    first = "started_at" not in cur
    STG.update_item(
        Key={"employee_id": eid, "stage": stage},
        UpdateExpression="SET #s = :p, task_token = :k, started_at = if_not_exists(started_at, :t)",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":p": "in_progress", ":k": token, ":t": now()})
    emp = get_emp(eid)
    if stage == "IT_PROVISIONING":
        laptop = "LAP-" + uuid.uuid4().hex[:6].upper()
        corp = re.sub(r"[^a-z.]", "", emp["name"].lower().replace(" ", ".")) + "@" + DOMAIN
        EMP.update_item(Key={"employee_id": eid}, UpdateExpression="SET laptop_id = :l, corporate_email = :c",
                        ExpressionAttributeValues={":l": laptop, ":c": corp})
        finish_stage(eid, stage)
    elif stage == "POLICY_SIGNOFF" and first:
        mail(emp["email"], "Action needed: sign company policies",
             f"<p>Hi {emp['name']}, your documents are verified. Please sign the policies in the portal: "
             f"<a href='{SITE}'>{SITE}</a></p>")
    elif stage == "MANAGER_INTRO" and first:
        alert_hr("Manager intro needed", f"Schedule the manager intro for {emp['name']} (manager: {emp['manager']}).")


def send_reminder(e, _):
    eid, stage = e["employee_id"], e["stage"]
    item = get_stage(eid, stage)
    if item.get("status") == "complete":
        return
    last = item.get("last_reminder_at")
    if last and (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() < 86000:
        return                                     # already reminded within the last 24h
    STG.update_item(Key={"employee_id": eid, "stage": stage},
                    UpdateExpression="SET last_reminder_at = :t ADD reminders_sent :one",
                    ExpressionAttributeValues={":one": 1, ":t": now()})
    emp = get_emp(eid)
    if stage in ("DOCUMENTS", "POLICY_SIGNOFF"):
        mail(emp["email"], f"Reminder: complete your {stage.replace('_', ' ').title()}",
             f"<p>Hi {emp['name']}, this stage of your onboarding is still pending. "
             f"<a href='{SITE}'>Open the portal</a>.</p>")
    else:
        alert_hr(f"Reminder: {stage}", f"{stage} is still pending for {emp['name']}.")


def finalize(e, _):
    eid = e["employee_id"]
    EMP.update_item(Key={"employee_id": eid}, UpdateExpression="SET overall_status = :r, ready_at = :t",
                    ExpressionAttributeValues={":r": "READY_FOR_DAY1", ":t": now()})
    emp = get_emp(eid)
    mail(emp["email"], "You're all set for Day 1! 🎉",
         f"<h2>Welcome to the team, {emp['name']}!</h2><p>Joining date: <b>{emp['joining_date']}</b><br>"
         f"Laptop: <b>{emp.get('laptop_id', '-')}</b><br>Work email: <b>{emp.get('corporate_email', '-')}</b></p>"
         f"<p>Sign in any time: <a href='{SITE}'>{SITE}</a></p>")
    alert_hr("Day-1 ready", f"{emp['name']} has completed onboarding and is ready for Day 1.")


# ------------------------------------------------------------------ S3 trigger
def validate_document(event, _):
    for rec in event["Records"]:
        key = urllib.parse.unquote_plus(rec["s3"]["object"]["key"])
        try:
            eid, doc_type, fname = key.split("/", 2)
        except ValueError:
            continue
        if doc_type not in DOC_TYPES:
            s3.delete_object(Bucket=BUCKET, Key=key)
            continue
        head = s3.head_object(Bucket=BUCKET, Key=key)
        sig = s3.get_object(Bucket=BUCKET, Key=key, Range="bytes=0-7")["Body"].read()
        ext = fname.rsplit(".", 1)[-1].lower()
        magic_ok = sig.startswith(b"%PDF") or sig.startswith(b"\x89PNG") or sig.startswith(b"\xff\xd8")
        reason = None
        if ext not in ALLOWED_EXT or not magic_ok:
            reason = "Unsupported or corrupt file type"
        elif head["ContentLength"] == 0 or head["ContentLength"] > MAX_BYTES:
            reason = "File must be under 5 MB"
        if reason:
            s3.delete_object(Bucket=BUCKET, Key=key)
        entry = {"status": "REJECTED" if reason else "VERIFIED", "file": fname, "at": now()}
        if reason:
            entry["reason"] = reason
        STG.update_item(Key={"employee_id": eid, "stage": "DOCUMENTS"},
                        UpdateExpression="SET docs.#t = :v",
                        ExpressionAttributeNames={"#t": doc_type}, ExpressionAttributeValues={":v": entry})
        item = get_stage(eid, "DOCUMENTS")
        docs = item.get("docs", {})
        missing = [d for d in DOC_TYPES if docs.get(d, {}).get("status") != "VERIFIED"]
        STG.update_item(Key={"employee_id": eid, "stage": "DOCUMENTS"},
                        UpdateExpression="SET #m = :m", ExpressionAttributeNames={"#m": "missing"},
                        ExpressionAttributeValues={":m": missing})
        if not missing and item.get("status") != "complete":
            emp = get_emp(eid)
            alert_hr("All documents received", f"All documents are received and verified for {emp['name']}. You can now release the offer letter from the HR dashboard.")
            finish_stage(eid, "DOCUMENTS")
