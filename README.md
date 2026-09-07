# teamspeak-ical-autoaway

Sets you away on TeamSpeak while your calendar says you're in a meeting.

Paste your calendars' ICS links into a config file. The tool sleeps until the next meeting, sets away with a message, sleeps until the meeting ends and clears it again, and re-fetches the calendars every few hours. Recurring events expand properly, exceptions and moved instances included. Cancelled events, free-time blocks, tentative ones and meetings you declined are ignored, overlapping meetings merge into one away block, and an away you set yourself is left alone.

Out of office is its own kind with its own message ("Out until Tue 09:00"). Outlook's show-as "Out of office" marks it, and so does a title starting with OOO for calendars that don't export show-as, Google included. Out of office wins over a meeting in the same span, and a day-long out of office block counts while other all-day events (birthdays, holidays) don't. A calendar entry can carry a default kind, `{ url = "...", kind = "ooo" }`, so a personal calendar where nothing is a meeting reads as out of office throughout.

## Requirements

- Python 3.11+, `icalendar`, `recurring-ical-events`
- TeamSpeak 3 with the ClientQuery plugin enabled (Tools > Options > Addons), or TeamSpeak 6 with Remote Apps enabled (Settings > Remote Apps)

## Setup

```
mkdir -p ~/.config/teamspeak-ical-autoaway
cp config.example.toml ~/.config/teamspeak-ical-autoaway/config.toml
```

Put your ICS links in `calendars` and enable `[teamspeak3]` or `[teamspeak6]`. Google has the link under a calendar's settings as "Secret address in iCal format", Outlook under Settings > Calendar > Shared calendars > Publish a calendar. The links grant read access to the calendar, so keep the file private.

## Run

```
pip install .
teamspeak-ical-autoaway
```

`--check` fetches the calendars once and prints the meetings it found for the next two days. `--config PATH` uses another config file.

## TeamSpeak 6

> [!WARNING]
> TeamSpeak 6 support is a workaround, and a poor one. The remote apps API can't set away, let alone a message. All it can do is send a virtual button press that you bind to the client's Away toggle. So the tool can only flip away on and off, and the message is whatever you typed under "Set Away Message" in the client. `{summary}` and `{end}` don't apply. If the message matters to you, use TeamSpeak 3.

1. `teamspeak-ical-autoaway --press away`. The client lists the app under Settings > Remote Apps > Permission Requests. Click Allow. The API key it returns is stored next to the config.
2. `teamspeak-ical-autoaway --press away --delay 10`, then switch to the client and click "+" on the Away row under Settings > Key Bindings before the press arrives. Recording stops if the client loses focus. The row then shows `com.zealsprince.teamspeak-ical-autoaway:away`.

The tool reads your away flag through the API before every press, so a Toggle binding never flips the wrong way, and a manual away or a manual "back" during a meeting stays as you set it. A press that changes nothing is logged as a warning; that means the binding is missing. The client's bundled docs call the message `keyPress`; the client only handles `buttonPress`.

## Nix

The flake exposes the package and a Home Manager module that runs it as a user service tied to the graphical session:

```nix
{
  inputs.teamspeak-ical-autoaway.url = "github:zealsprince/teamspeak-ical-autoaway";

  # in your Home Manager config
  imports = [ inputs.teamspeak-ical-autoaway.homeManagerModules.default ];
  services.teamspeak-ical-autoaway.enable = true;
}
```

Logs: `journalctl --user -u teamspeak-ical-autoaway`.

The nixpkgs `teamspeak6-client` wrapper leaves `libx11` and `libxi` off the library path, so the client's `hotkey_helper` segfaults on start and no key binding fires, keyboard or remote app. Until that's fixed upstream, wrap it:

```nix
(symlinkJoin {
  name = "teamspeak6-client";
  paths = [ teamspeak6-client ];
  nativeBuildInputs = [ makeWrapper ];
  postBuild = ''
    wrapProgram $out/bin/TeamSpeak \
      --prefix LD_LIBRARY_PATH : ${lib.makeLibraryPath [ libx11 libxi ]}
  '';
})
```

## License

MIT, see [LICENSE](LICENSE).
