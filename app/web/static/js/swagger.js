// app/web/static/js/swagger.js
/** Запускает self-hosted Swagger; интеграционные API-ключи не сохраняются между сессиями. */
window.ui = SwaggerUIBundle({ url: "/openapi.json", dom_id: "#swagger-ui", deepLinking: true,
  presets: [SwaggerUIBundle.presets.apis], persistAuthorization: false,
  defaultModelsExpandDepth: -1 });
