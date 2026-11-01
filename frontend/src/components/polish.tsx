import {
  Children,
  cloneElement,
  isValidElement,
  useId,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import { LayoutGroup, motion } from "motion/react";
import { GlowingEffect } from "./ui/glowing-effect";
function useQuietMotion() {
  const [reduced, setReduced] = useState(
    () => window.matchMedia("(prefers-reduced-motion: reduce)").matches,
  );
  useEffect(() => {
    const media = window.matchMedia("(prefers-reduced-motion: reduce)"),
      update = () => setReduced(media.matches);
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);
  return reduced;
}

// Restrained composition of Aceternity's free Glowing Effect and Motion layout transitions.
export function PolishedSurface({
  children,
  className = "",
}: {
  children: ReactNode;
  className?: string;
}) {
  const [hover, setHover] = useState(false);
  const reduced = useQuietMotion();
  return (
    <div
      className={`polished-surface ${className}`}
      onPointerEnter={(e) => setHover(e.pointerType === "mouse")}
      onPointerLeave={() => setHover(false)}
    >
      <GlowingEffect
        disabled={!hover || !!reduced}
        inactiveZone={0}
        proximity={12}
        movementDuration={0.35}
        spread={25}
        className="quiet-glow"
      />
      {children}
    </div>
  );
}
export function PolishedAsset({
  children,
  href,
}: {
  children: ReactNode;
  href: string;
}) {
  const [hover, setHover] = useState(false),
    reduced = useQuietMotion();
  return (
    <motion.a
      href={href}
      className="asset-tile polished-surface"
      whileHover={reduced ? undefined : { y: -3 }}
      transition={{ type: "spring", stiffness: 340, damping: 30 }}
      onPointerEnter={(e) => setHover(e.pointerType === "mouse")}
      onPointerLeave={() => setHover(false)}
    >
      <GlowingEffect
        disabled={!hover || !!reduced}
        inactiveZone={0}
        proximity={8}
        movementDuration={0.3}
        spread={22}
        className="quiet-glow"
      />
      {children}
    </motion.a>
  );
}
export function AnimatedSelection({
  active,
  children,
}: {
  active: string;
  children: ReactNode;
}) {
  const id = useId(),
    reduced = useQuietMotion();
  return (
    <LayoutGroup id={id}>
      {Children.map(children, (child) => {
        if (
          !isValidElement<{
            "data-selection-id"?: string;
            children?: ReactNode;
          }>(child)
        )
          return child;
        return cloneElement(
          child,
          {},
          <>
            {child.props["data-selection-id"] === active && (
              <motion.span
                className="selection-indicator"
                layoutId="selection"
                transition={
                  reduced
                    ? { duration: 0 }
                    : { type: "spring", stiffness: 430, damping: 38 }
                }
              />
            )}
            <span className="selection-label">{child.props.children}</span>
          </>,
        );
      })}
    </LayoutGroup>
  );
}
