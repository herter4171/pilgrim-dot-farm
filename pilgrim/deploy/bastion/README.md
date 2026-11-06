# Bastion (`pf`) — tailnet subnet router + LiteLLM relay

The old EC2 edge host. Two jobs now (SCALABILITY.md Step 8 and the Step 5
addendum); the edge nginx and `make edge-up` are retired.

1. **Subnet router** for the VPC CIDR, so the Mac reaches RDS and Valkey
   (`pilgrim/deploy/README.md` "Bastion subnet router").
2. **LiteLLM relay.** LiteLLM is a LAN service behind a public name and
   certificate; the Fargate tasks cannot reach it, and request moderation and
   paid summaries run on the web tier (RADIO §11). The relay is the retired
   edge path pointed at LiteLLM:

   ```
   task -> http://<bastion private ip>:4000  (this nginx, host network)
        -> https://justins-mac-studio.tail063158.ts.net   (tailscale serve on the M5)
        -> https://litellm.twr.church                     (LAN)
   ```

   Mac, once: `tailscale serve --bg --https=443 https://litellm.twr.church`
   (the tailnet grant `tag:radio-relay -> pilgrim-origin tcp:443` already
   allows the bastion in). Bastion: `MAC_TAILNET_NAME=... bash relay.sh start`
   then `bash relay.sh test` (a 401 from LiteLLM proves the chain). Security
   group: `pg-ec2` admits 4000 from `pg-ec2` (terraform
   `modules/ec2/security_groups.tf`). The web tier's `hosts.litellm` is the
   relay (`pilgrim/deploy/container/overlay.yaml`); the token travels in the
   `STRIPE_SECRET_ARN` document (RADIO §12). The dependency on the home LAN is
   the premise of the site (operator, 2026-11-05).
