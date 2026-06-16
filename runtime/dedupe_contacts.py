"""One-off: collapse duplicate-email rows in crm_master to one per email (merging the useful fields),
then add a UNIQUE index on lower(email) so duplicates can never be created again.
"""
from cortex import db


def main():
    groups = db.query("select lower(email) e, array_agg(id order by id) ids "
                      "from crm_master where email is not null and email <> '' "
                      "group by lower(email) having count(*) > 1")
    removed = 0
    for g in groups:
        ids = g["ids"]
        keep, dups = ids[0], ids[1:]
        rows = db.query("select organisation, is_client, account_id, note, company_name, phone, job_title "
                        "from crm_master where id = any(%s)", (ids,))
        orgs, is_client = set(), False
        account_id = note = company_name = phone = job_title = None
        for r in rows:
            for o in (r["organisation"] or "").split(","):
                if o.strip():
                    orgs.add(o.strip())
            is_client = is_client or bool(r["is_client"])
            account_id = account_id or r["account_id"]
            note = note or r["note"]
            company_name = company_name or r["company_name"]
            phone = phone or r["phone"]
            job_title = job_title or r["job_title"]
        db.execute("update crm_master set organisation=%s, is_client=%s, account_id=%s, note=%s, "
                   "company_name=%s, phone=%s, job_title=%s, updated_at=now() where id=%s",
                   (", ".join(sorted(orgs)), is_client, account_id, note, company_name, phone, job_title, keep))
        db.execute("delete from crm_master where id = any(%s)", (dups,))
        removed += len(dups)
    print(f"duplicate email groups: {len(groups)} | rows removed: {removed}")
    blanked = db.query("update crm_master set email=NULL where email is not null and btrim(email)='' returning id")
    print("blank emails set to NULL:", len(blanked))
    db.execute("create unique index if not exists crm_master_email_uniq on crm_master (lower(email))")
    print("unique index on lower(email) created")
    print("total crm_master now:", db.one("select count(*) n from crm_master")["n"])


if __name__ == "__main__":
    main()
