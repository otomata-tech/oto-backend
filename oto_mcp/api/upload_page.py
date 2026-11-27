"""La PAGE HTML d'un lien d'upload signé (#105) : le fallback humain quand l'agent n'a
pas de shell. Autoportée — aucun asset externe, la CSP (`entetes_securite.csp_upload`)
n'ouvre aucune origine et n'autorise que `SCRIPT_UPLOAD`, par son empreinte.

Sortie de `api/uploads.py`, qui garde la RÉCEPTION (handlers, plafonds, contrat) : la
page et la réception changent pour des raisons différentes.
"""
from __future__ import annotations

import html as _html


# Le seul script de la page, en constante : la CSP l'autorise par son EMPREINTE
# (`entetes_securite.csp_upload`), calculée sur cette chaîne même — le modifier sans
# que la politique suive est impossible. Tout ce qu'il affiche passe par
# `textContent` : ni le nom du fichier choisi ni le `detail` d'un refus n'entrent
# jamais dans le HTML. Ses textes suivent le `lang` de la page : un seul script pour
# les deux langues, donc une seule empreinte.
SCRIPT_UPLOAD = """
const f=document.getElementById("f"),fi=document.getElementById("file"),
z=document.getElementById("zone"),nm=document.getElementById("nom"),
b=document.getElementById("go"),m=document.getElementById("msg"),
ok=document.getElementById("ok"),u=document.getElementById("url");
const FR=document.documentElement.lang==="fr";
const T=FR?{retry:"Réessayer",sending:"Envoi…",copied:"Copié",
fail:c=>"L’envoi a échoué ("+c+").",net:"Erreur réseau : vérifie ta connexion puis réessaie.",
units:[" o"," Ko"," Mo"],comma:true,
msg:{token_already_used:"Ce lien a déjà servi. Demande à l’assistant d’en générer un nouveau.",
invalid_or_expired_token:"Ce lien a expiré. Demande à l’assistant d’en générer un nouveau.",
content_too_large:"Le fichier est trop volumineux pour ce lien.",
image_too_large:"L’image dépasse 2 Mo.",
unsupported_type:"Format non accepté : png, jpeg, gif ou webp seulement.",
empty_body:"Le fichier est vide.",missing_file:"Le fichier est vide.",
forbidden:"Ce compte n’a plus accès à la destination.",
unknown_project:"Le projet de destination n’existe plus.",
unknown_doc:"La page de destination n’existe plus.",
not_utf8:"Le fichier n’est pas un texte UTF-8."}}
:{retry:"Try again",sending:"Uploading…",copied:"Copied",
fail:c=>"Upload failed ("+c+").",net:"Network error: check your connection and try again.",
units:[" B"," KB"," MB"],comma:false,
msg:{token_already_used:"This link has already been used. Ask the assistant for a new one.",
invalid_or_expired_token:"This link has expired. Ask the assistant for a new one.",
content_too_large:"The file is too large for this link.",
image_too_large:"The image is larger than 2 MB.",
unsupported_type:"Unsupported format: png, jpeg, gif or webp only.",
empty_body:"The file is empty.",missing_file:"The file is empty.",
forbidden:"This account no longer has access to the destination.",
unknown_project:"The destination project no longer exists.",
unknown_doc:"The destination page no longer exists.",
not_utf8:"The file is not UTF-8 text."}};
const taille=n=>{if(n<1024){return n+T.units[0]}if(n<1048576){return (n/1024).toFixed(0)+T.units[1]}
const v=(n/1048576).toFixed(1);return (T.comma?v.replace(".",","):v)+T.units[2]};
const montre=()=>{const x=fi.files[0];z.classList.toggle("has",!!x);b.disabled=!x;
if(x){nm.textContent=x.name+" · "+taille(x.size)}m.hidden=true};
fi.addEventListener("change",montre);
["dragenter","dragover"].forEach(t=>z.addEventListener(t,e=>{e.preventDefault();z.classList.add("drag")}));
["dragleave","drop"].forEach(t=>z.addEventListener(t,()=>z.classList.remove("drag")));
z.addEventListener("drop",e=>{e.preventDefault();if(e.dataTransfer.files.length){fi.files=e.dataTransfer.files;montre()}});
const err=t=>{m.textContent=t;m.hidden=false;b.disabled=false;b.classList.remove("busy");b.textContent=T.retry};
f.addEventListener("submit",async e=>{e.preventDefault();if(!fi.files.length){return}
const fd=new FormData();fd.append("file",fi.files[0]);
m.hidden=true;b.disabled=true;b.classList.add("busy");b.textContent=T.sending;
try{const r=await fetch(location.href,{method:"POST",body:fd});
const j=await r.json().catch(()=>({}));
if(r.ok){f.hidden=true;ok.hidden=false;
if(j.url){u.hidden=false;u.querySelector("code").textContent=j.url;
u.querySelector("button").addEventListener("click",ev=>{navigator.clipboard.writeText(j.url);ev.target.textContent=T.copied})}}
else{const c=j.error||r.status;err((T.msg[c]||T.fail(c))+(j.detail?" "+j.detail:""))}
}catch(x){err(T.net)}});
"""

# Le script d'un GROS fichier (`large_uploads`) : le navigateur découpe le fichier en
# parties de `data-ps` octets, demande l'URL présignée de chacune, l'envoie AU STOCKAGE
# (jamais au backend), puis clôt. Trois essais par partie ; la progression s'affiche sur
# le bouton. Mêmes règles que `SCRIPT_UPLOAD` : tout passe par `textContent`.
SCRIPT_UPLOAD_LARGE = """
const f=document.getElementById("f"),fi=document.getElementById("file"),
z=document.getElementById("zone"),nm=document.getElementById("nom"),
b=document.getElementById("go"),m=document.getElementById("msg"),
ok=document.getElementById("ok");
const FR=document.documentElement.lang==="fr",PS=parseInt(f.dataset.ps,10);
const T=FR?{retry:"Réessayer",fail:c=>"L’envoi a échoué ("+c+").",
net:"Erreur réseau : vérifie ta connexion puis réessaie.",units:[" o"," Ko"," Mo"," Go"],comma:true,
msg:{token_already_used:"Ce lien a déjà servi. Demande à l’assistant d’en générer un nouveau.",
invalid_or_expired_token:"Ce lien a expiré. Demande à l’assistant d’en générer un nouveau.",
content_too_large:"Le fichier est trop volumineux pour ce lien.",
forbidden:"Ce compte n’a plus accès à la destination.",
unknown_project:"Le projet de destination n’existe plus."}}
:{retry:"Try again",fail:c=>"Upload failed ("+c+").",
net:"Network error: check your connection and try again.",units:[" B"," KB"," MB"," GB"],comma:false,
msg:{token_already_used:"This link has already been used. Ask the assistant for a new one.",
invalid_or_expired_token:"This link has expired. Ask the assistant for a new one.",
content_too_large:"The file is too large for this link.",
forbidden:"This account no longer has access to the destination.",
unknown_project:"The destination project no longer exists."}};
const taille=n=>{let i=0;while(n>=1024&&i<3){n/=1024;i++}
const v=i?n.toFixed(1):String(n);return (T.comma?v.replace(".",","):v)+T.units[i]};
const montre=()=>{const x=fi.files[0];z.classList.toggle("has",!!x);b.disabled=!x;
if(x){nm.textContent=x.name+" · "+taille(x.size)}m.hidden=true};
fi.addEventListener("change",montre);
["dragenter","dragover"].forEach(t=>z.addEventListener(t,e=>{e.preventDefault();z.classList.add("drag")}));
["dragleave","drop"].forEach(t=>z.addEventListener(t,()=>z.classList.remove("drag")));
z.addEventListener("drop",e=>{e.preventDefault();if(e.dataTransfer.files.length){fi.files=e.dataTransfer.files;montre()}});
const err=t=>{m.textContent=t;m.hidden=false;b.disabled=false;b.classList.remove("busy");b.textContent=T.retry};
const base=location.href.split(/[?#]/)[0];
const refus=async r=>{const j=await r.json().catch(()=>({}));const c=j.error||r.status;
return (T.msg[c]||T.fail(c))+(j.detail?" "+j.detail:"")};
const partie=async(x,i)=>{for(let k=0;k<3;k++){
const r=await fetch(base+"/part/"+i);if(!r.ok){throw new Error(await refus(r))}
try{const p=await fetch(await r.text(),{method:"PUT",body:x.slice((i-1)*PS,Math.min(i*PS,x.size))});
if(p.ok){return}}catch(e){}}throw new Error(T.net)};
f.addEventListener("submit",async e=>{e.preventDefault();const x=fi.files[0];if(!x){return}
m.hidden=true;b.disabled=true;b.classList.add("busy");
const n=Math.max(1,Math.ceil(x.size/PS));
try{for(let i=1;i<=n;i++){b.textContent=Math.floor(100*(i-1)/n)+" %";await partie(x,i)}
b.textContent="100 %";const r=await fetch(base+"/complete",{method:"POST"});
if(r.ok){f.hidden=true;ok.hidden=false}else{err(await refus(r))}
}catch(x){err(x.message||T.net)}});
"""

# Les jetons du design du tableau de bord (gris Radix Slate, le bleu d'Oto, rayons
# 6/8/12 px, corps 13 px). Pas de police distante : la page est autoportée, la CSP n'ouvre
# aucune origine — Inter s'il est installé, sinon la police du système.
_STYLE_UPLOAD = (
    ':root{color-scheme:light;--border:#d9d9e0;--muted:#60646c;--accent:#2d69d1;'
    '--g1:#fcfcfd;--g2:#f9f9fb;--g3:#f0f0f3;--g4:#e8e8ec;--g9:#8b8d98;--g12:#1c2024;'
    '--red:#ce2c31;--green:#218358}'
    '*{box-sizing:border-box}[hidden]{display:none!important}'
    '.avis{margin:12px 0 0;padding:8px 10px;border-radius:6px;background:#fff7e6;'
    'border:1px solid #f3d9a4;color:#7a4b00}'
    'body{margin:0;min-height:100vh;display:flex;flex-direction:column;align-items:center;'
    'justify-content:center;padding:24px 16px;background:var(--g2);color:var(--g12);'
    'font:13px/18px Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;'
    'letter-spacing:-.002em;-webkit-font-smoothing:antialiased}'
    '.card{width:100%;max-width:440px;background:#fff;border:1px solid var(--border);'
    'border-radius:12px;padding:24px;box-shadow:0 1px 2px #1c20240a,0 8px 24px #1c20240a}'
    '.ic{width:36px;height:36px;border-radius:8px;display:flex;align-items:center;'
    'justify-content:center;background:var(--g3);color:var(--muted);margin-bottom:14px}'
    '.ic.ok{background:#eaf6ef;color:var(--green)}.ic.ko{background:#fdeeee;color:var(--red)}'
    'h1{font-size:20px;line-height:28px;font-weight:500;letter-spacing:-.019em;margin:0 0 4px}'
    'p{margin:0;color:var(--muted)}p b{color:var(--g12);font-weight:500}'
    '.zone{position:relative;margin-top:20px;display:flex;flex-direction:column;align-items:center;gap:6px;'
    'padding:28px 16px;border:1px dashed #cdced6;border-radius:8px;background:var(--g1);'
    'text-align:center;cursor:pointer;transition:border-color .15s,background .15s}'
    '.zone:hover,.zone.drag{border-color:var(--accent);background:#f4f7fd}'
    '.zone:focus-within{outline:2px solid var(--accent);outline-offset:2px}'
    '.zone input{position:absolute;inset:0;opacity:0;cursor:pointer}'
    '.zone svg{color:var(--g9)}.zone .t{font-weight:500}.zone .s{color:var(--muted);font-size:12px}'
    '.zone .s u{color:var(--accent);text-decoration:none}'
    '.zone.has{border-style:solid;background:#fff}.zone.has svg{color:var(--accent)}'
    '.nom{display:none;font-weight:500;word-break:break-all}.zone.has .nom{display:block}'
    '.zone.has .t,.zone.has .s{display:none}'
    'button{font:inherit;font-weight:500;cursor:pointer;border-radius:6px}'
    '#go{width:100%;margin-top:12px;height:34px;border:0;background:var(--accent);color:#fff}'
    '#go:hover:not(:disabled){background:#2559b5}'
    '#go:disabled{background:var(--g4);color:var(--g9);cursor:default}'
    '#go.busy{background:var(--accent);color:#fff;opacity:.8}'
    '#go:focus-visible{outline:2px solid var(--accent);outline-offset:2px}'
    '.msg{margin-top:12px;padding:9px 12px;border-radius:6px;background:#fdeeee;'
    'border:1px solid #f6cfd0;color:var(--red)}'
    '.url{margin-top:16px;display:flex;gap:8px;align-items:center;padding:6px 6px 6px 10px;'
    'border:1px solid var(--border);border-radius:6px;background:var(--g1)}'
    '.url code{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;'
    'font:12px ui-monospace,SFMono-Regular,Menlo,monospace}'
    '.url button{border:1px solid var(--border);background:#fff;padding:3px 10px;color:var(--g12)}'
    '.url button:hover{background:var(--g3)}'
    '.foot{margin-top:16px;font-size:12px;color:var(--g9)}'
)

_ICONE_DEPOT = (
    '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m17 8-5-5-5 5"/>'
    '<path d="M12 3v12"/></svg>')
_ICONE_OK = (
    '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<path d="M20 6 9 17l-5-5"/></svg>')
_ICONE_EXPIRE = (
    '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>')


# Les textes de la page, par langue. L'anglais par défaut ; le français quand le
# navigateur le demande (`Accept-Language`, même règle que le tableau de bord).
_TEXTES_UPLOAD = {
    "en": {
        "title": "Upload a file", "dest": "Destination",
        "drop": "Drop your file here", "or": "or", "browse": "browse your computer",
        "file": "File", "send": "Upload",
        "foot": "Single-use link: it expires after the upload.",
        "ok": "File received", "ok_sub": "You can close this page and go back to the conversation.",
        "copy": "Copy",
        "expired": "Invalid or expired link",
        "expired_sub": "This upload link is no longer valid: it works once and expires after "
                       "a few minutes. Ask the assistant for a new one.",
    },
    "fr": {
        "title": "Déposer un fichier", "dest": "Destination",
        "drop": "Glisse ton fichier ici", "or": "ou", "browse": "parcours ton ordinateur",
        "file": "Fichier", "send": "Envoyer",
        "foot": "Lien à usage unique : il expire après le dépôt.",
        "ok": "Fichier reçu", "ok_sub": "Tu peux fermer cette page et revenir à la conversation.",
        "copy": "Copier",
        "expired": "Lien invalide ou expiré",
        "expired_sub": "Ce lien de dépôt n’est plus valable : il ne sert qu’une fois et expire "
                       "au bout de quelques minutes. Demande à l’assistant d’en générer un nouveau.",
    },
}


def upload_page_html(label: str | None, lang: str = "en", *,
                     part_size: int | None = None, notice: str | None = None) -> str:
    """Page d'upload autoportée d'un lien signé (#105, fallback humain). `label` None
    = lien invalide/expiré (message, sans formulaire). Le POST du fichier se fait vers
    la MÊME URL (multipart `file`), en fetch, avec accusé/erreur affiché. `lang` :
    `en` (défaut) ou `fr` ; toute autre valeur LÈVE (`KeyError`) : l'appelant la tire
    de `_locale_from_accept_language`, qui ne rend que ces deux-là."""
    t = _TEXTES_UPLOAD[lang]
    sep = "\u00a0:" if lang == "fr" else ":"
    if label is None:
        titre = t["expired"]
        body = (
            f'<main class="card"><div class="ic ko">{_ICONE_EXPIRE}</div>'
            f'<h1>{t["expired"]}</h1><p>{t["expired_sub"]}</p></main>')
    else:
        titre = t["title"]
        safe = _html.escape(label)
        # Gros fichier : la taille des parties, lue par `SCRIPT_UPLOAD_LARGE`.
        ps = f' data-ps="{int(part_size)}"' if part_size is not None else ""
        avis = f'<p class="avis">{_html.escape(notice)}</p>' if notice else ""
        body = (
            f'<main class="card"><form id="f"{ps}><div class="ic">{_ICONE_DEPOT}</div>'
            f'<h1>{t["title"]}</h1>'
            f'<p>{t["dest"]}{sep} <b>{safe}</b></p>'
            # La durée de garde d'un gros fichier, dite AVANT le dépôt et après.
            f'{avis}'
            '<label class="zone" id="zone">'
            f'{_ICONE_DEPOT}<span class="t">{t["drop"]}</span>'
            f'<span class="s">{t["or"]} <u>{t["browse"]}</u></span>'
            '<span class="nom" id="nom"></span>'
            f'<input type="file" name="file" id="file" required aria-label="{t["file"]}"></label>'
            f'<button type="submit" id="go" disabled>{t["send"]}</button>'
            '<p id="msg" class="msg" role="alert" hidden></p>'
            f'<p class="foot">{t["foot"]}</p></form>'
            f'<div id="ok" hidden><div class="ic ok">{_ICONE_OK}</div>'
            f'<h1>{t["ok"]}</h1><p>{t["ok_sub"]}</p>{avis}'
            '<div class="url" id="url" hidden><code></code>'
            f'<button type="button">{t["copy"]}</button></div></div></main>'
            f'<script>{SCRIPT_UPLOAD if part_size is None else SCRIPT_UPLOAD_LARGE}</script>')
    return (
        f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{titre}</title><style>{_STYLE_UPLOAD}</style></head>'
        f'<body>{body}</body></html>')
