// The public site's only script: copy the install command.
// release.sh keeps the tag here in step with install.sh and the README.
const INSTALL_CMD = "curl -fsSL https://github.com/Ga0512/SursumAI/raw/v0.8.5/install.sh | bash";

function copyInstall() {
  const btn = document.querySelector(".install-copy");
  const done = () => {
    if (btn) btn.textContent = "Copied!";
    setTimeout(() => { if (btn) btn.textContent = "Copy"; }, 2000);
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(INSTALL_CMD).then(done).catch(() => fallbackCopy(done));
  } else {
    fallbackCopy(done);
  }
}

function fallbackCopy(done) {
  const ta = document.createElement("textarea");
  ta.value = INSTALL_CMD;
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand("copy"); done(); } catch {}
  document.body.removeChild(ta);
}
