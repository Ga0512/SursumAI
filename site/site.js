// The public site's only script: copy the install command.
// release.sh keeps the tag here in step with install.sh and the README.
const INSTALL_CMD = "curl -fsSL https://github.com/Ga0512/SursumAI/raw/v1.0.22/install.sh | bash";

function copyInstall(button) {
  const term = button ? button.closest(".term") : document.querySelector(".term");
  const text = (term && term.dataset.copy) || INSTALL_CMD;
  const label = button ? button.querySelector("span") : null;

  const done = () => {
    if (!button) return;
    button.classList.add("copied");
    if (label) label.textContent = "Copied";
    setTimeout(() => {
      button.classList.remove("copied");
      if (label) label.textContent = "Copy";
    }, 1800);
  };

  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done).catch(() => fallbackCopy(text, done));
  } else {
    fallbackCopy(text, done);
  }
}

function fallbackCopy(text, done) {
  // older browsers, and any page served over plain http, where the clipboard
  // API is not available at all
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.setAttribute("readonly", "");
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand("copy"); } catch { /* nothing else to try */ }
  document.body.removeChild(ta);
  done();
}

/* The hero types through the models SursumAI can run, with each model's logo.
   Family names only, never versions: "Qwen3" is wrong the week Qwen4 lands, and
   the point is not one model anyway — it is that the pick is yours. */
const HERO_MODELS = [
  { name: "Qwen", logo: "https://avatars.githubusercontent.com/u/141221163?s=80" },
  { name: "DeepSeek", logo: "https://avatars.githubusercontent.com/u/148330874?s=80" },
  { name: "Kimi", logo: "https://avatars.githubusercontent.com/u/129152888?s=80" },
  { name: "Mistral", logo: "https://avatars.githubusercontent.com/u/132372032?s=80" },
  { name: "Muse-Glimmer", logo: "https://avatars.githubusercontent.com/u/153379578?s=80" },
  { name: "your fine-tune", logo: "" },
];

function startHeroRotator() {
  const box = document.getElementById("rotator");
  const nameEl = document.getElementById("rotatorName");
  const logoEl = document.getElementById("rotatorLogo");
  if (!box || !nameEl) return;

  // someone who asked the OS for less motion gets the list without the typing
  const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  let i = 0;

  const show = (model) => {
    if (model.logo) {
      logoEl.src = model.logo;
      logoEl.hidden = false;
    } else {
      logoEl.hidden = true;
    }
  };

  if (still) {
    setInterval(() => {
      i = (i + 1) % HERO_MODELS.length;
      show(HERO_MODELS[i]);
      nameEl.textContent = HERO_MODELS[i].name;
    }, 2600);
    return;
  }

  const type = async () => {
    for (;;) {
      const model = HERO_MODELS[i];
      show(model);
      for (let n = 1; n <= model.name.length; n++) {
        nameEl.textContent = model.name.slice(0, n);
        await wait(55);
      }
      await wait(1700);
      for (let n = model.name.length; n >= 0; n--) {
        nameEl.textContent = model.name.slice(0, n);
        await wait(28);
      }
      i = (i + 1) % HERO_MODELS.length;
    }
  };
  type();
}

const wait = (ms) => new Promise((r) => setTimeout(r, ms));

/* Sections arrive as you scroll to them — the page had no sense of motion. */
function revealOnScroll() {
  const targets = document.querySelectorAll(".landing-section, .landing-install, .landing-cta");
  if (!("IntersectionObserver" in window)) return;
  const seen = new IntersectionObserver((entries) => {
    for (const e of entries) {
      if (e.isIntersecting) { e.target.classList.add("seen"); seen.unobserve(e.target); }
    }
  }, { rootMargin: "0px 0px -10% 0px" });
  targets.forEach((t) => { t.classList.add("reveal"); seen.observe(t); });
}

document.addEventListener("DOMContentLoaded", () => {
  startHeroRotator();
  revealOnScroll();
});
