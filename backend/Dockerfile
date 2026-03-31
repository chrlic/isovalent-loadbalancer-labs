FROM nginx:1.27

# Generate a self-signed cert for the HTTPS server block
RUN mkdir -p /etc/nginx/certs && \
    openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
        -keyout /etc/nginx/certs/tls.key \
        -out    /etc/nginx/certs/tls.crt \
        -subj   "/CN=ilb-backend"

COPY nginx.conf /etc/nginx/nginx.conf

EXPOSE 8080 8443
