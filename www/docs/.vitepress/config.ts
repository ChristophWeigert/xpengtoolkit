import { defineConfig } from "vitepress"
import imsize from "markdown-it-imsize"

export default defineConfig({
  lang: "en-AU",
  title: "Xpengtk",
  base: "/xpengtoolkit/",
  description: "XPENG vehicles in Home Assistant.",
  lastUpdated: true,
  cleanUrls: true,
  head: [
    ["link", { rel: "icon", type: "image/svg+xml", href: "/logo.svg" }],
    ["meta", { name: "viewport", content: "width=device-width, initial-scale=1.0" }],
  ],
  themeConfig: {
    logo: "/logo.svg",
    nav: [
      { text: "Getting started", link: "/guide/overview", activeMatch: "/guide/" },
      { text: "Tutorials", link: "/tutorials/", activeMatch: "/tutorials/" },
      { text: "Reference", link: "/reference/options", activeMatch: "/reference/" },
      { text: "Examples", link: "/examples/overview", activeMatch: "/examples/" },
    ],
    sidebar: [
      {
        text: "Guide",
        items: [
          { text: "Overview", link: "/guide/overview" },
          {
            text: "Getting Started",
            link: "/guide/getting-started",
            items: [
              { text: "Certificates", link: "/guide/certificates" },
              { text: "Unattended renewal", link: "/guide/unattended-renewal" },
              { text: "Fault finding", link: "/guide/fault-finding" },
            ],
          },
          { text: "Notifications", link: "/guide/notifications" },
          { text: "Languages", link: "/guide/translations" },
          { text: "EVCC", link: "/guide/evcc" },
        ],
      },
      {
        text: "Tutorials",
        items: [
          { text: "Overview", link: "/tutorials/" },
          { text: "Vehicle card", link: "/tutorials/vehicle-card" },
        ],
      },
      {
        text: "Reference",
        items: [
          { text: "Integration options", link: "/reference/options" },
          { text: "Command permissions", link: "/reference/command-permissions" },
          { text: "Entities", link: "/reference/entities" },
          { text: "Services", link: "/reference/services" },
          { text: "App options", link: "/reference/app-options" },
        ],
      },
      {
        text: "Examples",
        items: [
          { text: "Overview", link: "/examples/overview" },
          { text: "Dashboard", link: "/examples/dashboard" },
          { text: "Automations", link: "/examples/automations" },
          { text: "Templates", link: "/examples/templates" },
        ],
      },
    ],
    editLink: {
      pattern: "https://github.com/xpengtoolkit/xpengtoolkit/edit/main/www/docs/:path",
      text: "Edit this page on GitHub",
    },
    socialLinks: [
      { icon: "github", link: "https://github.com/xpengtoolkit/xpengtoolkit" },
    ],
    footer: {
      message: "",
      copyright: "Apache 2.0 | Unofficial, not affiliated with XPENG",
    },
    search: {
      provider: "local",
    },
  },
  markdown: {
    config: (md) => {
      md.use(imsize as any)
    },
  },
  sitemap: {
    hostname: "https://xpengtoolkit.github.io/xpengtoolkit/",
  },
})
