# Network Fault Detection

Hi, this is my College Lab Project for Computer Networks.

I am building a software tool that runs on a laptop and tries to catch network problems early. Things like video calls freezing, games lagging, or websites feeling slow often come from Wi-Fi issues, DNS problems, ISP congestion, or the device itself. Most tools only tell you after something already broke, or they send a lot of data to the cloud.

My goal is to make a local app that:

- watches network health signals on the machine (Wi-Fi, DNS, path checks, simple probes)
- uses machine learning to predict when things might get bad soon
- explains the likely cause in simple words
- keeps everything on the device so user data is not sent to a cloud service

This project is software only. No special hardware is needed, just a normal laptop.

## Docs in this repo

- `explanation.md` - short, simple overview
- `Research.md` - research notes and paper references
- `PRD.md` - product requirements and build plan

I am still early in the build, so the docs come first and the code will follow.
