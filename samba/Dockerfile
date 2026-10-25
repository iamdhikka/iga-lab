FROM ubuntu:24.04

# samba-ad-provision (skema AD), samba-dsdb-modules, dan samba-vfs-modules wajib untuk
# domain controller; tanpa --no-install-recommends paket ini tidak ikut terpasang.
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      samba samba-ad-provision samba-ad-dc samba-dsdb-modules samba-vfs-modules \
      winbind krb5-user ldb-tools openssl ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && rm -f /etc/samba/smb.conf

COPY entrypoint.sh /entrypoint.sh
RUN sed -i 's/\r$//' /entrypoint.sh && chmod +x /entrypoint.sh

EXPOSE 389 636
ENTRYPOINT ["/entrypoint.sh"]
