{
  description = "Set yourself away on TeamSpeak while your calendar says you're in a meeting";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs =
    { self, nixpkgs }:
    let
      lib = nixpkgs.lib;
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];
      forEachSystem = f: lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});

      package =
        pkgs:
        pkgs.python3Packages.buildPythonApplication {
          pname = "teamspeak-ical-autoaway";
          version = "0.1.0";
          pyproject = true;
          src = ./.;
          build-system = [ pkgs.python3Packages.setuptools ];
          dependencies = with pkgs.python3Packages; [
            icalendar
            recurring-ical-events
          ];
          meta.mainProgram = "teamspeak-ical-autoaway";
        };
    in
    {
      packages = forEachSystem (pkgs: {
        default = package pkgs;
      });

      # `direnv allow` drops you into this: python with the runtime deps, so
      # `python teamspeak_ical_autoaway.py` runs straight from the checkout.
      devShells = forEachSystem (pkgs: {
        default = pkgs.mkShell {
          packages = [
            (pkgs.python3.withPackages (
              ps: with ps; [
                icalendar
                recurring-ical-events
              ]
            ))
          ];
        };
      });

      # Home Manager module: installs the package and runs it as a user service
      # that follows the graphical session. Configuration lives in
      # ~/.config/teamspeak-ical-autoaway/config.toml.
      homeManagerModules.default =
        {
          config,
          lib,
          pkgs,
          ...
        }:
        let
          cfg = config.services.teamspeak-ical-autoaway;
        in
        {
          options.services.teamspeak-ical-autoaway = {
            enable = lib.mkEnableOption "TeamSpeak away status from calendar meetings";
            package = lib.mkOption {
              type = lib.types.package;
              default = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
              description = "Package to run.";
            };
          };

          config = lib.mkIf cfg.enable {
            home.packages = [ cfg.package ];

            systemd.user.services.teamspeak-ical-autoaway = {
              Unit = {
                Description = "TeamSpeak away status from calendar meetings";
                After = [ "graphical-session.target" ];
                Wants = [ "graphical-session.target" ];
              };
              Service = {
                ExecStart = lib.getExe cfg.package;
                Restart = "always";
                RestartSec = "30s";
              };
              Install.WantedBy = [ "graphical-session.target" ];
            };
          };
        };
    };
}
