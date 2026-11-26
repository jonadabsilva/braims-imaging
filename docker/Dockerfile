# BRAIMS site pipeline image: BIDScoin 4.6.2 (dicomsort, bidsmapper, bidscoiner, dcm2niix, pydeface, FSL FLIRT),
# FSL robustfov, and the BRAIMS tools.  Build from the repository root:
#     docker build -f docker/Dockerfile -t braims .
FROM mambaorg/micromamba:1.5.10-bookworm-slim AS fsl
USER root
RUN micromamba create -y -p /opt/fslbet -c https://fsl.fmrib.ox.ac.uk/fsldownloads/fslconda/public/ -c conda-forge fsl-bet2 \
 && micromamba clean -a -y

FROM marcelzwiers/bidscoin:4.6.2
COPY --from=fsl /opt/fslbet /opt/fslbet
RUN ln -s /opt/fslbet/bin/robustfov /usr/local/bin/robustfov \
 && ln -s /opt/fslbet/bin/bet2 /usr/local/bin/bet2 \
 && pip install --no-cache-dir pillow scipy pyyaml
COPY braims /opt/braims
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "/opt/braims/braims.py"]
CMD ["run"]
