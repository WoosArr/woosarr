import { Link } from "react-router-dom";
import { api, type StatusOut } from "../api";
import { Icon } from "../icons";
import { Badge, ErrorBanner, PageHead, Spinner, useLoad } from "../ui";

export default function SetupPage() {
  const s = useLoad(() => api.get<StatusOut>("/api/v1/status"));
  if (s.error) return <ErrorBanner error={s.error} />;
  if (!s.data) return <Spinner />;
  const { plex, discord } = s.data;
  return (
    <>
      <PageHead title="Setup" sub="Connect WooSarr to your Plex server and Discord server." />
      <div className="grid cols-2">
        <section className="card stack" aria-labelledby="plex-setup">
          <div className="row spread">
            <h2 id="plex-setup" className="row"><Icon name="plex" /> Plex</h2>
            {plex.connected ? <Badge tone="ok">Connected</Badge> : <Badge tone="neutral">Not set up</Badge>}
          </div>
          <ol className="steps small">
            <li><strong>Sign in with Plex.</strong> WooSarr opens plex.tv; you approve access there. No password is typed into WooSarr.</li>
            <li><strong>Pick your server.</strong> WooSarr remembers its machine id.</li>
            <li><strong>Check libraries and users.</strong> WooSarr reads them and tests that it can read each user's shares.</li>
          </ol>
          <div className="banner info">
            <Icon name="info" />
            <div className="small">
              The Plex wizard is being finished and tested against a real server. Until then WooSarr can't read or change
              Plex shares, and nothing on your server is touched.
            </div>
          </div>
        </section>
        <section className="card stack" aria-labelledby="discord-setup">
          <div className="row spread">
            <h2 id="discord-setup" className="row"><Icon name="discord" /> Discord</h2>
            {discord.connected ? <Badge tone="ok">Connected</Badge> : <Badge tone="neutral">Not set up</Badge>}
          </div>
          <ol className="steps small">
            <li>Create an application at <a href="https://discord.com/developers/applications" target="_blank" rel="noreferrer">discord.com/developers</a> and add a <strong>Bot</strong>.</li>
            <li>Turn on the <strong>Server Members Intent</strong> (Bot → Privileged Gateway Intents).</li>
            <li>Copy the bot token and paste it here. It's stored encrypted and never shown again.</li>
            <li>Invite the bot to your server with the link WooSarr generates, then pick the server.</li>
          </ol>
          <div className="banner info">
            <Icon name="info" />
            <div className="small">The Discord wizard and the <code>/link</code> command arrive with the bot integration.</div>
          </div>
        </section>
      </div>
      <p className="small muted" style={{ marginTop: 16 }}>
        Until both are connected, you can prepare role mappings and exemptions; WooSarr stays in dry-run until you turn it off in{" "}
        <Link to="/settings">Settings</Link>.
      </p>
    </>
  );
}
