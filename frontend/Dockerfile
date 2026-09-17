FROM nginx:1.27-alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY public/ /usr/share/nginx/html/

EXPOSE 80

HEALTHCHECK --interval=15s --timeout=4s --start-period=5s --retries=3 \
    CMD wget -q --spider http://localhost/ || exit 1
