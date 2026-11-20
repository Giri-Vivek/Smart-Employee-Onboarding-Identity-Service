# DynamoDB schema (paste into https://mermaid.live to export PNG)
```mermaid
erDiagram
  EMPLOYEES ||--|{ ONBOARDING_STAGES : "has 4"
  EMPLOYEES {
    string employee_id PK "UUID"
    string name
    string email
    string department
    string role
    string manager
    string joining_date
    string employment_type
    string overall_status "ONBOARDING | READY_FOR_DAY1"
    string laptop_id
    string corporate_email
    string created_at
  }
  ONBOARDING_STAGES {
    string employee_id PK
    string stage SK "DOCUMENTS | IT_PROVISIONING | POLICY_SIGNOFF | MANAGER_INTRO"
    string status "pending | in_progress | complete"
    string task_token
    number reminders_sent
    map docs "DOCUMENTS stage only"
    list missing "DOCUMENTS stage only"
    string started_at
    string completed_at
  }
```
