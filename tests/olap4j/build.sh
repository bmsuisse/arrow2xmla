#!/bin/bash
# Builds olap4j's XMLA driver (an independent XMLA client) for tests/e2e/test_olap4j.py.
#   JAVA_HOME=/path/to/jdk tests/olap4j/build.sh [OUT_DIR]      (needs a JDK 11+, git and curl)
# olap4j (Apache-2.0) is fetched and built here, not vendored. The driver is not on Maven Central, so it is compiled
# from source against the released olap4j-1.2.0 jar.
set -euo pipefail
OUT=${1:-.olap4j-build}
JAVA=${JAVA_HOME:+$JAVA_HOME/bin/}
mkdir -p "$OUT/jars" "$OUT/classes" "$OUT/gen/org/olap4j/driver/xmla"
cd "$OUT"
M=https://repo1.maven.org/maven2
[ -f jars/olap4j-1.2.0.jar ] || curl -sfL -o jars/olap4j-1.2.0.jar $M/org/olap4j/olap4j/1.2.0/olap4j-1.2.0.jar
[ -f jars/xercesImpl.jar ] || curl -sfL -o jars/xercesImpl.jar $M/xerces/xercesImpl/2.12.2/xercesImpl-2.12.2.jar
[ -f jars/xml-apis.jar ] || curl -sfL -o jars/xml-apis.jar $M/xml-apis/xml-apis/1.4.01/xml-apis-1.4.01.jar
[ -d src ] || git clone -q --depth 1 https://github.com/olap4j/olap4j.git src
S=src/src/org/olap4j
sed -e 's/@pomversion@/1.3.0/' -e 's/@buildtime@/test/' src/src/resources/version/XmlaOlap4jDriverVersion.java.template > gen/org/olap4j/driver/xmla/XmlaOlap4jDriverVersion.java
# the JDBC 3 / 4.0 factories do not compile on a current JDK and are not used on one
${JAVA}javac -nowarn -cp jars/olap4j-1.2.0.jar:jars/xercesImpl.jar:jars/xml-apis.jar -d classes \
  $(find $S/driver/xmla -name '*.java' ! -name FactoryJdbc4Impl.java ! -name FactoryJdbc3Impl.java) \
  $S/metadata/DisplayFolder.java gen/org/olap4j/driver/xmla/XmlaOlap4jDriverVersion.java 2>&1 | grep -v '^Note' || true
echo "built: $PWD"
