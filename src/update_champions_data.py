import luigi
import requests
import json
import time
import os

class ChampionsDownload(luigi.ExternalTask):
    champions_path = luigi.Parameter(default="champions.json")
    def output(self):
        # output is temporary file to force task to check if patch data matches
        return luigi.LocalTarget("tmp/pipeline/champions{}.json".format(time.time()))

    def run(self):
        url = "https://ddragon.leagueoflegends.com/api/versions.json"
        response = requests.get(url=url).json()
        current_patch = response[0]
        # Check for local file patch version
        try:
            with open(self.champions_path, 'r') as infile:
                data = json.load(infile)
                local_patch = data["version"]
        except:
                local_patch = None

        # update local file if patches do not match (uses temporary file)
        if local_patch != current_patch:
            print("Local patch does not match current patch.. Updating")
            url = "http://ddragon.leagueoflegends.com/cdn/{current_patch}/data/en_US/champion.json".format(current_patch=current_patch)
            response = requests.get(url=url).json()
            tmp_file = self.output().path
            with open(tmp_file, 'w') as outfile:
                json.dump(response, outfile)
            os.rename(tmp_file, self.champions_path)
        else:
            print("Local patch matches current patch.. Skipping")
        return

if __name__ == "__main__":
    luigi.run(['ChampionsDownload', "--local-scheduler"])
