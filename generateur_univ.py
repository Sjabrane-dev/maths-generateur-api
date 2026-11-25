# Fichier : API_RENDER/generateur_univ.py
"""Générateur universitaire : feuilles d'exercices tirées de la banque Exo7 (classes prépa, M1-M2).

Routes :
  GET  /univ/chapitres                 -> chapitres (année, filières, nombre d'exercices)
  GET  /univ/exercices/{chapitre}      -> exercices d'un chapitre (énoncé, indication, correction)
  POST /univ/generer-pdf               -> PDF (énoncés, indications, corrigés au choix)

Les exercices viennent d'Exo7 (exo7.emath.fr), sous licence Creative Commons BY-NC-SA :
chaque feuille cite la source, l'identifiant et l'auteur de chaque exercice.
"""
import json
import os
from datetime import datetime
import shutil
import subprocess
import tempfile
import threading
import time

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

router = APIRouter(prefix="/univ")

ICI = os.path.dirname(os.path.abspath(__file__))
CHEMIN_BANQUE = os.path.join(ICI, "banque_univ.json")
MAX_EXERCICES = 30
DELAI_COMPILATION = 90
LIMITE_PAR_MINUTE = 6                    # feuilles par adresse IP et par minute (contre les abus)

_verrou = threading.Semaphore(1)         # une seule compilation à la fois sur le serveur
_historique = {}


def _trop_de_demandes(ip):
    maintenant = time.time()
    liste = [t for t in _historique.get(ip, []) if maintenant - t < 60]
    trop = len(liste) >= LIMITE_PAR_MINUTE
    if not trop:
        liste.append(maintenant)
    _historique[ip] = liste
    if len(_historique) > 5000:          # ménage : on oublie les adresses inactives
        for cle in [k for k, v in _historique.items() if not v or maintenant - v[-1] > 60]:
            del _historique[cle]
    return trop

with open(CHEMIN_BANQUE, encoding="utf-8") as f:
    _DONNEES = json.load(f)
CHAPITRES = _DONNEES["chapitres"]                     # liste ordonnée
EXERCICES = {e["id"]: e for e in _DONNEES["exercices"]}
PAR_CHAPITRE = {}
for e in _DONNEES["exercices"]:
    PAR_CHAPITRE.setdefault(e["chapitre"], []).append(e)


def echapper(texte):
    return "".join(
        {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_",
         "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}.get(c, c)
        for c in (texte or ""))


from generateur import (generer_preambule, generer_page_presentation, generer_page_chapitre,
                        MACROS_CHAPITRE, FOOTER, annee_scolaire, TEXTES_CALCULATRICE, formater_points)

# commandes de la banque Exo7 (ajoutées au préambule du générateur du lycée)
MACROS_EXO7 = r"""
\usepackage{fancybox}
\newcommand{\Nn}{\mathbb{N}}\newcommand{\Zz}{\mathbb{Z}}\newcommand{\Qq}{\mathbb{Q}}
\newcommand{\Rr}{\mathbb{R}}\newcommand{\Cc}{\mathbb{C}}\newcommand{\Kk}{\mathbb{K}}
\renewcommand{\epsilon}{\varepsilon}
\renewcommand{\ge}{\geqslant}\renewcommand{\geq}{\geqslant}\renewcommand{\le}{\leqslant}\renewcommand{\leq}{\leqslant}
\providecommand{\dd}{\mathrm{d}}
\providecommand{\ch}{\mathop{\mathrm{ch}}\nolimits}\providecommand{\sh}{\mathop{\mathrm{sh}}\nolimits}
\renewcommand{\tanh}{\mathop{\mathrm{th}}\nolimits}\providecommand{\cotan}{\mathop{\mathrm{cotan}}\nolimits}
\providecommand{\Argsh}{\mathop{\mathrm{argsh}}\nolimits}\providecommand{\Argch}{\mathop{\mathrm{argch}}\nolimits}
\providecommand{\Argth}{\mathop{\mathrm{argth}}\nolimits}\providecommand{\pgcd}{\mathop{\mathrm{pgcd}}\nolimits}
\providecommand{\llbracket}{[\![}\providecommand{\rrbracket}{]\!]}
\definecolor{col2}{RGB}{248,193,12}
\fancyfoot[C]{\scriptsize Exercices Exo7 -- CC BY-NC-SA}
\newcommand{\SourceExo}[1]{\hfill{\normalfont\scriptsize\color{gray}#1}}
"""


MACROS_SUPERIEUR = r"""
% ----- modèle « Supérieur » : pastilles numérotées, petites capitales, filets fins
\renewenvironment{exercice}[2][]{%
    \stepcounter{exocount}\par\vspace{0.55cm}\noindent
    \tikz[baseline=(n.base)]\node[fill=col1,text=white,font=\sffamily\bfseries\small,minimum size=6mm,inner sep=0pt,rounded corners=1.5pt](n){\arabic{exocount}};%
    \enspace{\color{col1}\scshape #2}{\color{col1}\small\itshape #1}
    \par\nopagebreak\vspace{0.15cm}\noindent{\color{col1!35}\rule{\linewidth}{0.4pt}}\par\nopagebreak\vspace{0.1cm}
}{\par}
\newcommand{\PastilleCorrige}[2]{\par\vspace{0.5cm}\noindent
    \tikz[baseline=(n.base)]\node[draw=col1,text=col1,font=\sffamily\bfseries\small,minimum size=6mm,inner sep=0pt,rounded corners=1.5pt,line width=0.8pt](n){#1};%
    \enspace{\color{col1}\scshape Corrigé -- #2}\par\nopagebreak\vspace{0.1cm}}
\newcommand{\PastilleIndication}[1]{\par\vspace{0.4cm}\noindent
    \tikz[baseline=(n.base)]\node[draw=col2,fill=col2!15,text=col1,font=\sffamily\bfseries\small,minimum size=6mm,inner sep=0pt,rounded corners=1.5pt](n){#1};%
    \enspace{\color{col1}\scshape Indication}\par\nopagebreak\vspace{0.1cm}}
"""


def entete_superieur(titre, classe, etab, date_du_jour, duree, texte_calc, total, exos):
    """En-tête sobre des feuilles de TD et de colle du supérieur."""
    infos = []
    if duree:
        infos.append("Durée : " + echapper(duree))
    if texte_calc:
        infos.append(texte_calc)
    if total > 0:
        infos.append("Barème : " + formater_points(total))
    infos.append(str(len(exos)) + " exercice" + ("s" if len(exos) > 1 else ""))
    infos.append(date_du_jour)
    themes = list(dict.fromkeys(e.get("theme", "") for e in exos if e.get("theme")))
    ligne_themes = ""
    if themes:
        ligne_themes = (r"\par\vspace{0.35cm}{\small\color{col1}\textsc{Thèmes} :\enspace}{\small " +
                        r" \textcolor{col2}{$\bullet$} ".join(echapper(t) for t in themes[:6]) + "}")
    return r"""
\thispagestyle{premiere}
\noindent{\color{col1}\rule{\textwidth}{2pt}}\par\vspace{-0.6mm}
\noindent{\color{col2}\rule{\textwidth}{0.8pt}}\par\vspace{1.5mm}
\noindent{\small\color{col1}\scshape """ + echapper(etab) + r"""}\hfill{\small\color{col1}\scshape Année universitaire """ + annee_scolaire().replace("-", "--") + r"""}
\par\vspace{0.9cm}
\begin{center}
{\color{col1}\fontsize{24}{28}\selectfont\scshape """ + echapper(titre) + r"""}\\[3mm]
""" + (r"{\large\itshape " + echapper(classe) + r"}\\[3mm]" if classe else "") + r"""
{\color{col2}\rule{3cm}{1.2pt}}\\[2.5mm]
{\small\color{col1}""" + r"\enspace\textcolor{col2}{$\diamond$}\enspace ".join(infos) + r"""}
\end{center}
""" + ligne_themes + r"""
\par\vspace{0.3cm}\noindent{\color{col1}\rule{\textwidth}{0.6pt}}
"""


def source(e):
    return r"Exo7 n\textsuperscript{o}\,%s -- %s" % (e["id"].split("-")[1], echapper(e["auteur"]))


def titre_exo(e):
    t = e.get("titre") or echapper(e.get("theme", ""))
    if e.get("difficulte"):
        t += r"\enspace$" + r"\star" * e["difficulte"] + "$"
    return t


def construire_tex(ids, titre, classe, corrections, indications, etablissement="", duree="",
                   calculatrice="", page_presentation=False, style_presentation="moderne",
                   citation="", auteur_citation="", points=None):
    """Feuille au format du générateur du lycée : en-tête, Nom/Prénom, page de présentation au choix."""
    exos = [EXERCICES[i] for i in ids]
    etab = etablissement.strip() or "Mathématiques -- Supérieur"
    titre = titre.strip() or "Feuille d'exercices"
    if corrections == "seuls":
        titre += " — Corrigé"
    classe = classe.strip()
    date_du_jour = datetime.now().strftime("%d/%m/%Y")
    texte_calc = TEXTES_CALCULATRICE.get((calculatrice or "").strip(), "")
    pts = {i: float(v) for i, v in (points or {}).items() if i in ids and 0 < float(v) <= 100}
    total = sum(pts.values())

    code = generer_preambule(titre, etab).replace("\\begin{document}", MACROS_EXO7 + "\n\\begin{document}", 1)
    if page_presentation and style_presentation == "chapitre":
        code = code.replace("\\begin{document}", MACROS_CHAPITRE + "\n\\begin{document}", 1)
    superieur = style_presentation == "superieur" and not page_presentation
    if superieur:
        code = code.replace("\\begin{document}", MACROS_SUPERIEUR + "\n\\begin{document}", 1)

    if superieur:
        code += entete_superieur(titre, classe, etab, date_du_jour, duree.strip(), texte_calc, total, exos)
    elif page_presentation:
        fabrique = generer_page_chapitre if style_presentation == "chapitre" else generer_page_presentation
        page = fabrique(titre, etab, classe, date_du_jour, duree.strip(), total, len(exos),
                        citation.strip(), auteur_citation.strip(), texte_calc)
        # adaptations au supérieur : année universitaire, pas de mention « LPO »
        page = page.replace("Année scolaire", "Année universitaire")
        page = page.replace(r"{\Large\bfseries\sffamily LPO}", r"{\Large\bfseries\sffamily SUP}")
        code += page
    else:
        ligne_duree = (r"\\[2mm] {\large Durée : " + echapper(duree.strip()) + "}") if duree.strip() else ""
        if texte_calc:
            ligne_duree += r"\\[1mm] {\itshape " + texte_calc + "}"
        code += r"""
\thispagestyle{premiere}
\noindent
\begin{minipage}[t]{0.5\textwidth}
    \textbf{""" + echapper(etab) + r"""} \\
    Année universitaire """ + annee_scolaire() + r"""
\end{minipage}
\begin{minipage}[t]{0.5\textwidth}
    \raggedleft
    \textbf{""" + echapper(classe) + r"""} \\
    \textbf{Date : } """ + date_du_jour + ((r" \\ \textbf{Barème : } " + formater_points(total)) if total > 0 else "") + r"""
\end{minipage}

\vspace{0.3cm}
\noindent{\color{col1}\rule{\textwidth}{0.8pt}}
\vspace{0.2cm}

\begin{center}
    {\LARGE \bfseries \color{col1} """ + echapper(titre) + r"""}""" + ligne_duree + r"""
\end{center}

\vspace{0.1cm}
\noindent{\color{col1}\rule{\textwidth}{1.5pt}}
\vspace{0.4cm}
""" + ("" if corrections == "seuls" else r"""
\noindent
\textbf{Nom : }\dotfill\qquad\textbf{Prénom : }\dotfill\mbox{}

\vspace{0.4cm}
""")

    def titre_partie(texte):
        return (r"\newpage\begin{center}{\LARGE \bfseries \color{col1} " + texte +
                r"}\end{center}\noindent{\color{col1}\rule{\textwidth}{1.5pt}}\vspace{0.2cm}" + "\n")

    if corrections != "seuls":
        for e in exos:
            bareme = ("[ (" + formater_points(pts[e["id"]]) + ")]") if e["id"] in pts else ""
            code += "\\begin{exercice}" + bareme + "{" + titre_exo(e) + r"\SourceExo{" + source(e) + "}}\n"
            code += e["enonce"] + "\n\\end{exercice}\n"
    if indications and corrections != "seuls" and any(e.get("indication") for e in exos):
        code += titre_partie("Indications")
        for n, e in enumerate(exos, 1):
            if e.get("indication") and superieur:
                code += r"\PastilleIndication{" + str(n) + "}\n" + e["indication"] + "\n\\par\n"
            elif e.get("indication"):
                code += (r"\vspace{0.4cm}\noindent{\color{col1}\textbf{Indication pour l'exercice " + str(n) +
                         r"}}\par\vspace{0.1cm}" + "\n" + e["indication"] + "\n\\par\n")
    if corrections in ("fin", "seuls"):
        if corrections == "fin":
            code += titre_partie("Corrigés")
        for n, e in enumerate(exos, 1):
            if superieur:
                code += r"\PastilleCorrige{" + str(n) + "}{" + titre_exo(e) + r"\SourceExo{" + source(e) + "}}\n"
                code += (e.get("correction") or r"\emph{Pas de corrigé disponible dans la banque Exo7 pour cet exercice.}")
                code += "\n\\par\n"
                continue
            code += (r"\vspace{0.4cm}\noindent{\color{col1}\textbf{Corrigé de l'exercice " + str(n) +
                     r"}} \textbf{-- " + titre_exo(e) + r"\SourceExo{" + source(e) + r"}}\par\vspace{0.1cm}" + "\n")
            code += (e.get("correction") or r"\emph{Pas de corrigé disponible dans la banque Exo7 pour cet exercice.}")
            code += "\n\\par\n"
    code += (r"\par\vfill\begin{center}\scriptsize\color{gray} Exercices tirés de la banque Exo7 "
             r"(exo7.emath.fr), licence Creative Commons BY-NC-SA.\end{center}" + "\n")
    return code + FOOTER


IMAGES = os.path.join(ICI, "images_exo7")     # figures d'Exo7, appelées « ../images/… »


def compiler(tex):
    racine = tempfile.mkdtemp(prefix="univ_")
    dossier = os.path.join(racine, "doc")
    os.makedirs(dossier)
    if os.path.isdir(IMAGES):
        os.symlink(IMAGES, os.path.join(racine, "images"))
    with open(os.path.join(dossier, "fiche.tex"), "w", encoding="utf-8") as f:
        f.write(tex)
    try:
        for _ in range(2):
            subprocess.run(["pdflatex", "-interaction=nonstopmode", "fiche.tex"], cwd=dossier,
                           capture_output=True, timeout=DELAI_COMPILATION)
    except subprocess.TimeoutExpired:
        shutil.rmtree(racine, ignore_errors=True)
        raise HTTPException(status_code=500, detail="La compilation a pris trop de temps.")
    pdf = os.path.join(dossier, "fiche.pdf")
    if not os.path.exists(pdf):
        shutil.rmtree(racine, ignore_errors=True)
        raise HTTPException(status_code=500, detail="La compilation LaTeX a échoué.")
    return pdf, racine


class DemandeUniv(BaseModel):
    exercices: list[str] = Field(min_length=1, max_length=MAX_EXERCICES)
    titre: str | None = Field(default="Feuille d'exercices", max_length=120)
    sous_titre: str | None = Field(default="", max_length=120)     # classe / filière, ex. « MP -- Réduction »
    etablissement: str | None = Field(default="", max_length=120)
    duree: str | None = Field(default="", max_length=40)
    calculatrice: str | None = Field(default="", max_length=20)     # "", "autorisee" ou "interdite"
    page_presentation: bool = False
    style_presentation: str | None = Field(default="moderne", max_length=20)   # "moderne", "chapitre" ou "superieur"
    citation: str | None = Field(default="", max_length=300)
    auteur_citation: str | None = Field(default="", max_length=80)
    corrections: str | None = Field(default="sans", max_length=10)  # "sans", "fin" ou "seuls"
    indications: bool = False
    points: dict[str, float] | None = None   # barème facultatif : {"exo7-001611": 4, ...}


@router.get("/chapitres")
def univ_chapitres():
    return [dict(c, nombre=len(PAR_CHAPITRE.get(c["cle"], [])),
                 corriges=sum(1 for e in PAR_CHAPITRE.get(c["cle"], []) if e.get("correction")))
            for c in CHAPITRES]


@router.get("/exercices/{chapitre}")
def univ_exercices(chapitre: str):
    if chapitre not in PAR_CHAPITRE:
        raise HTTPException(status_code=404, detail="Chapitre inconnu.")
    return PAR_CHAPITRE[chapitre]


@router.post("/generer-pdf")
def univ_generer_pdf(demande: DemandeUniv, x_forwarded_for: str | None = Header(default=None)):
    ip = (x_forwarded_for or "inconnue").split(",")[0].strip()
    if _trop_de_demandes(ip):
        raise HTTPException(status_code=429, detail="Trop de feuilles demandées : patientez une minute.")
    inconnus = [i for i in demande.exercices if i not in EXERCICES]
    if inconnus:
        raise HTTPException(status_code=400, detail="Exercice inconnu : " + inconnus[0])
    mode = demande.corrections if demande.corrections in ("sans", "fin", "seuls") else "sans"
    tex = construire_tex(list(dict.fromkeys(demande.exercices)), demande.titre or "", demande.sous_titre or "",
                         mode, demande.indications, demande.etablissement or "", demande.duree or "",
                         demande.calculatrice or "", demande.page_presentation,
                         demande.style_presentation or "moderne", demande.citation or "", demande.auteur_citation or "",
                         demande.points)
    if not _verrou.acquire(timeout=60):
        raise HTTPException(status_code=503, detail="Le serveur est occupé : réessayez dans un instant.")
    try:
        pdf, dossier = compiler(tex)
    finally:
        _verrou.release()
    return FileResponse(pdf, media_type="application/pdf", filename="feuille_universitaire.pdf",
                        background=BackgroundTask(shutil.rmtree, dossier, ignore_errors=True))
