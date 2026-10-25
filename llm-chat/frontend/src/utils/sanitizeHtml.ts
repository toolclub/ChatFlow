import DOMPurify from 'dompurify'

/** Final boundary for untrusted generated HTML, after all renderer transforms.
 * Keep DOMPurify's HTML/SVG/MathML defaults for tables, code icons and KaTeX.
 * Stylesheets are unnecessary inside content and must not restyle the app.
 */
export function sanitizeHtml(html: string): string {
  return DOMPurify.sanitize(html, {
    FORBID_TAGS: ['style'],
    RETURN_TRUSTED_TYPE: false,
  })
}
