import type { RefObject } from "react";

type HeroStatLabel = {
  label: string;
  value: string;
};

export type HeroEntryOption = {
  key: string;
  label: string;
  active: boolean;
  onSelect: () => void;
};

export type HeroEntryMenu = {
  ref: RefObject<HTMLDivElement>;
  open: boolean;
  onToggle: () => void;
  options: HeroEntryOption[];
};

type LobbyHeroProps = {
  visible: boolean;
  menuOpen: boolean;
  screen: "home" | "create" | "list" | "room" | "game";
  eyebrow: string;
  title: string;
  subtitle: string;
  isServer: boolean;
  chips: number | string;
  bonusChips: number;
  secondaryLabel: HeroStatLabel | null;
  entryMenu: HeroEntryMenu | null;
};

export default function LobbyHero({
  visible,
  menuOpen,
  screen,
  eyebrow,
  title,
  subtitle,
  isServer,
  chips,
  bonusChips,
  secondaryLabel,
  entryMenu,
}: LobbyHeroProps) {
  if (!visible) return null;

  const compact = screen !== "home";

  return (
    <header className={`hero-card hero-card--compact hero-card--landscape ${compact ? "hero-card--subpage" : "hero-card--home"} ${menuOpen ? "hero-card--menu-open" : ""}`}>
      <div className="hero-card__copy">
        <span className="hero-card__eyebrow">{eyebrow}</span>
        <h1>{title}</h1>
        <p>{subtitle}</p>
      </div>
      {isServer ? (
        <div className="hero-card__meta hero-card__meta--hud">
          <div className="hero-stat hero-stat--chips">
            <span>Fichas</span>
            <strong>{chips}</strong>
          </div>
          {bonusChips > 0 ? (
            <div className="hero-stat hero-stat--bonus">
              <span>Bônus</span>
              <strong>{bonusChips}</strong>
            </div>
          ) : null}
          {secondaryLabel ? (
            entryMenu ? (
              <div
                ref={entryMenu.ref}
                className={`entry-selector entry-selector--hero entry-selector--hero-compact ${entryMenu.open ? "entry-selector--open" : ""}`}
              >
                <button
                  className="entry-selector__trigger entry-selector__trigger--hero"
                  type="button"
                  onClick={entryMenu.onToggle}
                >
                  <span className="entry-selector__trigger-copy">
                    <span className="entry-selector__label">{secondaryLabel.label}</span>
                    <strong>{secondaryLabel.value}</strong>
                  </span>
                  <span className={`entry-selector__chevron ${entryMenu.open ? "entry-selector__chevron--open" : ""}`}>v</span>
                </button>
                <div className={`entry-selector__menu entry-selector__menu--hero ${entryMenu.open ? "entry-selector__menu--open" : ""}`}>
                  {entryMenu.options.map((option) => (
                    <button
                      key={option.key}
                      type="button"
                      className={`entry-selector__option ${option.active ? "entry-selector__option--active" : ""}`}
                      onClick={option.onSelect}
                    >
                      {option.label}
                    </button>
                  ))}
                </div>
              </div>
            ) : (
              <div className="hero-stat hero-stat--entry">
                <span>{secondaryLabel.label}</span>
                <strong>{secondaryLabel.value}</strong>
              </div>
            )
          ) : null}
        </div>
      ) : null}
    </header>
  );
}
