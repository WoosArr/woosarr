/** A Discord-style message button for previews (reaction posts, the link help DM). */
export type ButtonStyle = "primary" | "secondary" | "success" | "danger";

export function DiscordButtonRow({ label, style }: { label: string; style: ButtonStyle }) {
  return <div className="discord-button-row"><span className={`discord-button ${style}`}>{label}</span></div>;
}
