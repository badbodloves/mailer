#!/usr/bin/env python3
"""Streamlit Web-GUI for the Mass Mailer system. Run: streamlit run gui.py"""
import os, sys, time, json, sqlite3, configparser, threading
from datetime import datetime, timedelta
import streamlit as st
import psutil

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mailer.mailer_core import MailerCore
from mailer.db_manager import DBManager
from mailer.content_engine import ContentEngine
from mailer.mime_builder import MIMEBuilder
from mailer.antifingerprint import AntiFingerprintEngine

st.set_page_config(page_title="Mass Mailer", page_icon="📧", layout="wide")
CONFIG_PATH = "config.ini"
LEADS_DIR, SMTPS_DIR, HTML_DIR, LOGOS_DIR = "Leads", "SMTPs", "html_bodies", "logos"
LOG_FILE, REDIRECT_DB = "smtp_errors.log", "redirects.db"
for _d in (LEADS_DIR, SMTPS_DIR, HTML_DIR, LOGOS_DIR):
    os.makedirs(_d, exist_ok=True)

def _scan(folder, ext=".txt"):
    if not os.path.isdir(folder): return []
    return sorted(f for f in os.listdir(folder) if f.lower().endswith(ext))

def _scan_images(folder):
    if not os.path.isdir(folder): return []
    return sorted(f for f in os.listdir(folder) if f.lower().endswith((".png",".jpg",".jpeg",".gif",".webp")))

def _rcfg():
    cp = configparser.ConfigParser()
    if os.path.isfile(CONFIG_PATH): cp.read(CONFIG_PATH, encoding="utf-8")
    return cp

def _wcfg(cp):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f: cp.write(f)

def _db_stats(p):
    r = {"PENDING":0,"SENT":0,"FAILED":0,"IN_PROGRESS":0,"total":0}
    if not os.path.isfile(p): return r
    try:
        c = sqlite3.connect(p, timeout=5); c.execute("PRAGMA busy_timeout=5000")
        for s,n in c.execute("SELECT state,COUNT(*) FROM leads GROUP BY state").fetchall(): r[s]=n
        r["total"]=c.execute("SELECT COUNT(*) FROM leads").fetchone()[0]; c.close()
    except Exception: pass
    return r

def _smtp_list(path):
    out=[]
    if not os.path.isfile(path): return out
    try:
        for ln in open(path,"r",encoding="utf-8"):
            ln=ln.strip()
            if not ln or ln.startswith("#"): continue
            p=ln.split(",")
            if len(p)>=4: out.append({"Host":p[0].strip(),"Port":p[1].strip(),"User":p[2].strip()})
    except OSError: pass
    return out

def _log_tail(n=50):
    if not os.path.isfile(LOG_FILE): return "(no log file)"
    try:
        lines=open(LOG_FILE,"r",encoding="utf-8",errors="replace").readlines()
        return "".join(lines[-n:]) or "(empty)"
    except: return "(error)"

def _redirect_links():
    if not os.path.isfile(REDIRECT_DB): return []
    try:
        c=sqlite3.connect(REDIRECT_DB,timeout=5)
        rows=c.execute("SELECT short_url,created_at FROM redirect_links ORDER BY id DESC LIMIT 100").fetchall()
        c.close(); return [{"URL":r[0],"Created":r[1]} for r in rows]
    except: return []

def _add_redirect(url):
    c=sqlite3.connect(REDIRECT_DB,timeout=5)
    c.execute("CREATE TABLE IF NOT EXISTS redirect_links(id INTEGER PRIMARY KEY AUTOINCREMENT,short_url TEXT NOT NULL,target_url TEXT NOT NULL,created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    c.execute("INSERT INTO redirect_links(short_url,target_url) VALUES(?,?)",(url,"manual"))
    c.commit(); c.close()

def _gen_sample_mail(html_src, cp):
    try:
        ce=ContentEngine(html_dir=HTML_DIR,attachments_dir="",spintax_dir=cp.get("paths","spintax_dir",fallback="spintaxes"),
            names_file=cp.get("paths","names_file",fallback=""),subjects_file=cp.get("paths","subjects_file",fallback=""),
            alt_texts_file=cp.get("paths","alt_texts_file",fallback=""))
        af=AntiFingerprintEngine(enable_classes=False)
        email="preview@example.com"
        from_name=ce.process(cp.get("sender","from_name",fallback="Test"),email)
        subject=ce.process(cp.get("sender","subject",fallback="Test Subject"),email)
        html=af.transform(ce.process(html_src,email))
        plain=ContentEngine.html_to_plaintext(html)
        from_email=cp.get("sender","from_email",fallback="") or "noreply@example.com"
        raw=MIMEBuilder.build_email(from_name=from_name,from_email=from_email,to_email=email,subject=subject,html_body=html,plain_body=plain)
        return html, raw, None
    except Exception as e:
        return None, None, str(e)

def _run_mailer(overrides):
    try:
        core=MailerCore(config_path=CONFIG_PATH,overrides=overrides)
        st.session_state["_core"]=core
        st.session_state["mailer_error"]=""
        core.run()
    except Exception as e:
        st.session_state["mailer_error"]=str(e)
    finally:
        st.session_state["running"]=False
        st.session_state["_core"]=None

for k,v in {"running":False,"started_at":0.0,"mailer_error":"","_core":None,"scheduler_time":None,
    "html_preview":"","raw_preview":"","log_auto":False,"gui_log":[]}.items():
    if k not in st.session_state: st.session_state[k]=v

st.title("📧 Mass Mailer Control Panel")
cp=_rcfg()
db_path=cp.get("database","db_path",fallback="mailer.db")

tab_camp, tab_edit, tab_logos, tab_redir, tab_cfg, tab_logs = st.tabs(
    ["🚀 Campaign","📝 HTML Editor","🖼 Logos","🔗 Redirects","⚙️ Config","📋 Logs"])

# ─── TAB: CAMPAIGN ───
with tab_camp:
    col_ctrl, col_stats = st.columns([1,2])
    with col_ctrl:
        st.subheader("File Selection")
        lf=_scan(LEADS_DIR); sf=_scan(SMTPS_DIR)
        sel_l=st.selectbox("Lead List",lf if lf else ["(empty)"],disabled=st.session_state["running"])
        sel_s=st.selectbox("SMTP Pool",sf if sf else ["(empty)"],disabled=st.session_state["running"])
        st.divider()
        st.subheader("Upload")
        ut=st.radio("To",["Leads/","SMTPs/"],horizontal=True)
        uf=st.file_uploader(".txt file",type=["txt"],key="up1")
        if uf:
            td=LEADS_DIR if ut=="Leads/" else SMTPS_DIR
            open(os.path.join(td,uf.name),"wb").write(uf.getvalue())
            st.success(f"Saved {uf.name}"); st.rerun()
        st.divider()
        st.subheader("Control")
        c1,c2,c3=st.columns(3)
        with c1:
            if st.button("▶ START",type="primary",use_container_width=True,disabled=st.session_state["running"] or not lf or not sf):
                ov={"paths.leads_file":os.path.join(LEADS_DIR,sel_l),"paths.smtp_file":os.path.join(SMTPS_DIR,sel_s)}
                st.session_state["running"]=True; st.session_state["started_at"]=time.time(); st.session_state["mailer_error"]=""
                threading.Thread(target=_run_mailer,args=(ov,),daemon=True).start(); st.rerun()
        with c2:
            if st.button("⏸ PAUSE",use_container_width=True,disabled=not st.session_state["running"]):
                core=st.session_state.get("_core")
                if core: core.stop()
                st.info("Paused — pending leads preserved. Press START to resume.")
        with c3:
            if st.button("⏹ FORCE STOP",type="secondary",use_container_width=True,disabled=not st.session_state["running"]):
                core=st.session_state.get("_core")
                if core: core.force_stop()
                st.session_state["running"]=False
                st.warning("Force stopped. IN_PROGRESS reset to PENDING.")
        if st.session_state["mailer_error"]:
            st.error(st.session_state["mailer_error"])
        st.divider()
        st.subheader("Scheduler")
        sd=st.date_input("Date",value=datetime.now().date())
        stm=st.time_input("Time",value=datetime.now().time())
        if st.button("Schedule",disabled=st.session_state["running"] or not lf or not sf):
            tgt=datetime.combine(sd,stm); dl=(tgt-datetime.now()).total_seconds()
            if dl>0:
                st.session_state["scheduler_time"]=tgt
                st.success(f"Scheduled: {tgt:%Y-%m-%d %H:%M}")
                def _sched():
                    time.sleep(dl)
                    if not st.session_state["running"]:
                        ov={"paths.leads_file":os.path.join(LEADS_DIR,sel_l),"paths.smtp_file":os.path.join(SMTPS_DIR,sel_s)}
                        st.session_state["running"]=True; st.session_state["started_at"]=time.time()
                        threading.Thread(target=_run_mailer,args=(ov,),daemon=True).start()
                threading.Thread(target=_sched,daemon=True).start()
            else: st.warning("Past time.")
        if st.session_state["scheduler_time"]:
            rem=(st.session_state["scheduler_time"]-datetime.now()).total_seconds()
            if rem>0: st.info(f"⏰ {int(rem//60)}m {int(rem%60)}s")
            else: st.session_state["scheduler_time"]=None

    with col_stats:
        st.subheader("Live Dashboard")
        stats=_db_stats(db_path); total=stats["total"]; sent=stats["SENT"]; failed=stats["FAILED"]
        pending=stats["PENDING"]; inprog=stats["IN_PROGRESS"]; processed=sent+failed
        m1,m2,m3,m4=st.columns(4)
        m1.metric("Total",total); m2.metric("Sent",sent); m3.metric("Failed",failed); m4.metric("Pending",pending+inprog)
        st.progress(processed/total if total>0 else 0, text=f"{processed/total*100 if total else 0:.1f}%")
        el=time.time()-st.session_state["started_at"] if st.session_state["running"] else 0
        spd=processed/el if el>0 else 0; rem_s=(total-processed)/spd if spd>0 else 0
        s1,s2,s3=st.columns(3)
        s1.metric("Speed",f"{spd:.1f} m/s"); s2.metric("ETA",str(timedelta(seconds=int(rem_s))) if spd>0 else "--:--")
        s3.metric("Status","🟢 Running" if st.session_state["running"] else "⚪ Idle")
        if sent+failed>0:
            import plotly.graph_objects as go
            fig=go.Figure(data=[go.Pie(labels=["Sent","Failed"],values=[sent,failed],marker=dict(colors=["#00cc66","#ff4444"]),hole=0.4)])
            fig.update_layout(height=220,margin=dict(t=10,b=10,l=10,r=10)); st.plotly_chart(fig,use_container_width=True)
        st.divider()
        st.subheader("System")
        sy1,sy2=st.columns(2)
        sy1.metric("CPU",f"{psutil.cpu_percent(interval=0):.0f}%"); sy2.metric("RAM",f"{psutil.virtual_memory().percent:.0f}%")
        st.divider()
        st.subheader("SMTP Pool")
        sp=os.path.join(SMTPS_DIR,sel_s) if sf else ""
        al=_smtp_list(sp)
        if al: st.dataframe(al,use_container_width=True)
        else: st.info("No SMTPs loaded.")
    if st.session_state["running"]: time.sleep(2); st.rerun()

# ─── TAB: HTML EDITOR ───
with tab_edit:
    st.subheader("HTML Editor")
    hfiles=_scan(HTML_DIR,(".html",".htm"))
    sel_h=st.selectbox("Template",["(new)"]+hfiles,key="tpl_sel")
    existing=""
    if sel_h!="(new)":
        try: existing=open(os.path.join(HTML_DIR,sel_h),"r",encoding="utf-8").read()
        except: pass
    html_src=st.text_area("HTML Source",value=existing,height=350,key="html_ed")
    ec1,ec2,ec3,ec4=st.columns(4)
    with ec1:
        sname=st.text_input("Filename",value=sel_h if sel_h!="(new)" else "template.html")
    with ec2:
        if st.button("💾 Save Template"):
            open(os.path.join(HTML_DIR,sname),"w",encoding="utf-8").write(html_src)
            st.success(f"Saved {sname}")
    with ec3:
        if st.button("👁 Preview HTML"):
            html_out,raw_out,err=_gen_sample_mail(html_src,cp)
            if err: st.error(err)
            else: st.session_state["html_preview"]=html_out; st.session_state["raw_preview"]=raw_out
    with ec4:
        if st.button("📨 Full Mail Preview"):
            html_out,raw_out,err=_gen_sample_mail(html_src,cp)
            if err: st.error(err)
            else: st.session_state["html_preview"]=html_out; st.session_state["raw_preview"]=raw_out
    if st.session_state["html_preview"]:
        st.divider()
        ptab1,ptab2=st.tabs(["Rendered HTML","Raw MIME Source"])
        with ptab1:
            st.components.v1.html(st.session_state["html_preview"],height=500,scrolling=True)
        with ptab2:
            if st.session_state["raw_preview"]:
                st.code(st.session_state["raw_preview"][:5000],language="text")

# ─── TAB: LOGOS ───
with tab_logos:
    st.subheader("Logo Manager")
    imgs=_scan_images(LOGOS_DIR)
    if imgs:
        cols=st.columns(min(len(imgs),4))
        for i,img in enumerate(imgs):
            with cols[i%4]:
                fpath=os.path.join(LOGOS_DIR,img)
                st.image(fpath,caption=img,use_container_width=True)
    else:
        st.info(f"No images in {LOGOS_DIR}/")
    st.divider()
    st.subheader("Upload Logo")
    logo_up=st.file_uploader("Image file",type=["png","jpg","jpeg","gif","webp"],key="logo_up")
    if logo_up:
        dest=os.path.join(LOGOS_DIR,logo_up.name)
        open(dest,"wb").write(logo_up.getvalue())
        st.success(f"Saved {dest}"); st.rerun()
    st.divider()
    st.subheader("Cloudinary Status")
    ccp=_rcfg()
    api_on=ccp.get("IMAGE_API","enabled",fallback="false").lower() in ("true","1","yes")
    cname=ccp.get("CLOUDINARY","cloud_name",fallback="")
    st.write(f"**Enabled:** {'Yes' if api_on else 'No'} | **Cloud:** {cname or '(not set)'}")
    pool_file="image_pool.json"
    if os.path.isfile(pool_file):
        try:
            urls=json.load(open(pool_file,"r"))
            st.metric("Cached URLs",len(urls) if isinstance(urls,list) else 0)
            if isinstance(urls,list) and urls:
                with st.expander(f"Show URLs ({len(urls)})"):
                    st.code("\n".join(urls[:50]),language="text")
                    if len(urls)>50: st.caption(f"... and {len(urls)-50} more")
        except: st.warning("Cache file corrupt.")
    else:
        st.info("No image_pool.json yet. URLs are generated on first run with IMAGE_API enabled.")
    if st.button("🗑 Clear Image Cache"):
        if os.path.isfile(pool_file): os.unlink(pool_file); st.success("Cache cleared.")

# ─── TAB: REDIRECTS ───
with tab_redir:
    st.subheader("Redirect Manager")
    rcp=_rcfg()
    r_on=rcp.get("redirect","enabled",fallback="false").lower() in ("true","1","yes")
    r_url=rcp.get("redirect","target_url",fallback="")
    st.write(f"**Enabled:** {'Yes' if r_on else 'No'} | **Target:** {r_url or '(not set)'}")
    links=_redirect_links()
    st.metric("Links in DB",len(links))
    if links:
        with st.expander(f"Show links ({len(links)})"):
            st.dataframe(links,use_container_width=True)
    st.divider()
    st.subheader("Add Links Manually")
    manual_url=st.text_input("Short URL (e.g. https://share.google/...)",key="redir_manual")
    if st.button("➕ Add Link") and manual_url.strip():
        try:
            _add_redirect(manual_url.strip()); st.success("Added."); st.rerun()
        except Exception as e: st.error(str(e))
    st.divider()
    st.subheader("Bulk Add")
    bulk=st.text_area("One URL per line",height=150,key="redir_bulk")
    if st.button("➕ Add All"):
        added=0
        for ln in bulk.strip().splitlines():
            ln=ln.strip()
            if ln:
                try: _add_redirect(ln); added+=1
                except: pass
        if added: st.success(f"Added {added} links."); st.rerun()
    st.divider()
    if st.button("🗑 Clear All Redirects"):
        if os.path.isfile(REDIRECT_DB):
            c=sqlite3.connect(REDIRECT_DB); c.execute("DELETE FROM redirect_links"); c.commit(); c.close()
            st.success("All links deleted."); st.rerun()

# ─── TAB: CONFIG ───
with tab_cfg:
    st.subheader("Config Editor")
    cpe=_rcfg()
    with st.form("cfg_form"):
        c1,c2=st.columns(2)
        with c1:
            st.markdown("**Sending**")
            threads=st.number_input("Threads",1,200,cpe.getint("sending","threads",fallback=40))
            ndelay=st.number_input("Normal Delay",0.0,60.0,cpe.getfloat("sending","normal_delay",fallback=0.3),step=0.1)
            pdelay=st.number_input("Provider Delay",0.0,60.0,cpe.getfloat("sending","provider_delay",fallback=6.0),step=0.5)
            wdelay=st.number_input("Warmup Delay",0.0,120.0,cpe.getfloat("sending","warmup_delay",fallback=30.0),step=5.0)
            issl=st.checkbox("Ignore SSL Errors",cpe.get("sending","ignore_ssl_errors",fallback="true").lower() in ("true","1","yes"))
        with c2:
            st.markdown("**Sender**")
            fname=st.text_input("From Name",cpe.get("sender","from_name",fallback="{from_name}"))
            femail=st.text_input("From Email (empty=SMTP)",cpe.get("sender","from_email",fallback=""))
            subj=st.text_input("Subject",cpe.get("sender","subject",fallback=""))
            st.markdown("**Cloudinary**")
            img_en=st.checkbox("Image API",cpe.get("IMAGE_API","enabled",fallback="false").lower() in ("true","1","yes"))
            cl_name=st.text_input("Cloud Name",cpe.get("CLOUDINARY","cloud_name",fallback=""))
            cl_key=st.text_input("API Key",cpe.get("CLOUDINARY","api_key",fallback=""))
            cl_sec=st.text_input("API Secret",cpe.get("CLOUDINARY","api_secret",fallback=""),type="password")
            st.markdown("**Redirects**")
            rd_en=st.checkbox("Redirects",cpe.get("redirect","enabled",fallback="false").lower() in ("true","1","yes"))
            rd_url=st.text_input("Target URL",cpe.get("redirect","target_url",fallback=""))
        if st.form_submit_button("💾 Save",type="primary"):
            for sec in ("sending","sender","test","content","IMAGE_API","CLOUDINARY","redirect"):
                if not cpe.has_section(sec): cpe.add_section(sec)
            cpe.set("sending","threads",str(int(threads))); cpe.set("sending","normal_delay",str(ndelay))
            cpe.set("sending","provider_delay",str(pdelay)); cpe.set("sending","warmup_delay",str(wdelay))
            cpe.set("sending","ignore_ssl_errors",str(issl)); cpe.set("sender","from_name",fname)
            cpe.set("sender","from_email",femail); cpe.set("sender","subject",subj)
            cpe.set("IMAGE_API","enabled",str(img_en)); cpe.set("CLOUDINARY","cloud_name",cl_name)
            cpe.set("CLOUDINARY","api_key",cl_key); cpe.set("CLOUDINARY","api_secret",cl_sec)
            cpe.set("redirect","enabled",str(rd_en)); cpe.set("redirect","target_url",rd_url)
            _wcfg(cpe); st.success("Config saved.")
    st.divider()
    st.subheader("Database")
    d1,d2=st.columns(2)
    with d1:
        if st.button("🔄 Reset IN_PROGRESS"):
            try: db=DBManager(db_path); db.reset_in_progress(); db.close(); st.success("Done.")
            except Exception as e: st.error(str(e))
    with d2:
        if st.button("🗑 Delete DB",type="secondary"):
            if os.path.isfile(db_path): os.unlink(db_path); st.success("Deleted.")

# ─── TAB: LOGS ───
with tab_logs:
    st.subheader("Error Log")
    la=st.toggle("Auto-refresh",value=st.session_state["log_auto"],key="la_t")
    st.session_state["log_auto"]=la
    st.code(_log_tail(50),language="text")
    if st.button("🗑 Clear Log"):
        if os.path.isfile(LOG_FILE): open(LOG_FILE,"w").close(); st.success("Cleared.")
    if la: time.sleep(2); st.rerun()
